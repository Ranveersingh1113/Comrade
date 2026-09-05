"""Direct-Postgres access for the backend workers.

Every worker connects under an RLS-enforced role (never service_role) and scopes
itself to a single team per transaction via SET LOCAL app.current_team_id. The
policies in 0002_rls.sql then constrain every row the worker can see or write.
"""
import atexit
import json
from contextlib import contextmanager
from enum import Enum
from typing import Iterator

import psycopg
from psycopg_pool import ConnectionPool, PoolTimeout

from .config import settings


class Role(str, Enum):
    ADMIN = "admin"          # tests only — table owner, bypasses RLS
    AGENT = "agent"          # reads + proposes + private nudges
    EXECUTOR = "executor"    # performs approved consent actions only
    PIPELINE = "pipeline"    # document parser + memory compiler


_URLS: dict[Role, str] = {
    Role.ADMIN: settings.comrade_db_url_admin,
    Role.AGENT: settings.comrade_agent_db_url,
    Role.EXECUTOR: settings.comrade_executor_db_url,
    Role.PIPELINE: settings.comrade_pipeline_db_url,
}

# how each worker is recorded in change_log (via app.actor_kind GUC)
_ACTOR_KIND: dict[Role, str] = {
    Role.AGENT: "ai",
    Role.EXECUTOR: "ai",     # executes the AI's approved action
    Role.PIPELINE: "compiler",
}


# One pool per distinct URL, opened on first use. findings §3.2: the rule is
# "never open a connection per request" and this codebase opened one per
# OPERATION — a 20-step agent turn cost 20 connect/close cycles in
# append_step alone, and long-running turns are coming.
#
# Pooling is safe here only because every scoping statement below is
# TRANSACTION-scoped (SET LOCAL / set_config(..., true)). The scope dies with
# the transaction, so a recycled connection cannot hand the next borrower the
# previous borrower's team or identity. Changing any of them to session scope
# would be a silent cross-tenant leak — tests/test_db_pool.py guards this by
# borrowing a bare connection after a scoped one and asserting it is clean.
#
# ponytail: fixed size, no per-role tuning. Size from measurement if a role
# starts queueing.
_POOL_MIN, _POOL_MAX = 1, 10
_pools: dict[str, ConnectionPool] = {}
# Turn locks live for the whole model call. They cannot borrow the agent-work
# pool: a full lock pool would otherwise deadlock every holder on start_run.
_lock_pool: ConnectionPool | None = None
_LOCK_POOL_MAX = 32


def _reset(conn: psycopg.Connection) -> None:
    """Normalise a connection on its way back to the pool.

    pipeline/worker.py borrows via connect() and sets autocommit itself, so a
    returned connection can carry autocommit=True. Clearing it here keeps that
    caller's choice from becoming the next borrower's default.
    """
    conn.rollback()
    conn.autocommit = False


def _pool(url: str) -> ConnectionPool:
    pool = _pools.get(url)
    if pool is None:
        pool = ConnectionPool(
            url, min_size=_POOL_MIN, max_size=_POOL_MAX, reset=_reset, open=True
        )
        _pools[url] = pool
    return pool


def _advisory_lock_pool() -> ConnectionPool:
    global _lock_pool
    if _lock_pool is None:
        pool = ConnectionPool(
            _URLS[Role.AGENT], min_size=_POOL_MIN, max_size=_LOCK_POOL_MAX,
            reset=_reset, open=True,
        )
        # A near-zero borrow must still work for the first turn. Warm the pool
        # once here; later saturation returns busy without a material stall.
        pool.wait()
        _lock_pool = pool
    return _lock_pool


def close_pools() -> None:
    """Close every pool. For clean shutdown and test teardown."""
    for pool in _pools.values():
        pool.close()
    _pools.clear()
    global _lock_pool
    if _lock_pool is not None:
        _lock_pool.close()
        _lock_pool = None


atexit.register(close_pools)


@contextmanager
def connect(role: Role) -> Iterator[psycopg.Connection]:
    """Borrow a connection as the given role. Caller manages transactions."""
    with _pool(_URLS[role]).connection() as conn:
        yield conn


@contextmanager
def thread_lock(thread_id: str) -> Iterator[bool]:
    """Hold one thread's turn lock for the duration of the block.

    A thread is the agent's shared working surface. Its turns must stay ordered
    so each sees the one before it; unrelated threads stay concurrent.

    Yields True if this caller holds the thread, False if someone else does. The
    caller decides what to say — decision Q6 is that a queued member gets an
    honest line, not a spinner.

    Session-scoped (`pg_try_advisory_lock`), not transaction-scoped: a turn
    spans several LLM calls, and holding a transaction across them would break
    the project-wide "no LLM call inside a transaction" rule. That means the
    lock lives on one borrowed connection for the whole turn, and the `finally`
    is what makes it safe — an unreleased advisory lock would wedge the room
    until the connection was recycled.

    ponytail: lock pool caps at 32 active turns; the worker pool remains free
    for run/step writes. Replace with the leased queue in Task 9.
    """
    # hashtextextended gives a 64-bit key, so distinct IDs collide far less
    # often than with the 32-bit hashtext. A collision only serialises two
    # unrelated threads — harmless, but confusing to debug.
    try:
        with _advisory_lock_pool().connection(timeout=0.01) as conn:
            # Session advisory locks deliberately span LLM calls. Keep this
            # borrowed connection out of a transaction while it waits for them.
            conn.autocommit = True
            acquired = conn.execute(
                "select pg_try_advisory_lock(hashtextextended(%s, 0))", (str(thread_id),)
            ).fetchone()[0]
            try:
                yield bool(acquired)
            finally:
                if acquired:
                    conn.execute(
                        "select pg_advisory_unlock(hashtextextended(%s, 0))",
                        (str(thread_id),),
                    )
    except PoolTimeout:
        yield False


@contextmanager
def team_session(role: Role, team_id: str) -> Iterator[psycopg.Connection]:
    """Open a worker connection scoped to one team for one transaction.

    SET LOCAL app.current_team_id binds current_team() in the RLS policies, so
    every read/write inside the block is confined to `team_id`. The transaction
    commits on clean exit and rolls back on error.
    """
    if role is Role.ADMIN:
        raise ValueError("team_session is for worker roles, not ADMIN")
    with _pool(_URLS[role]).connection() as conn:
        with conn.transaction():
            conn.execute(
                "select set_config('app.current_team_id', %s, true)", (str(team_id),)
            )
            conn.execute(
                "select set_config('app.actor_kind', %s, true)", (_ACTOR_KIND[role],)
            )
            yield conn


@contextmanager
def user_session(user_id: str) -> Iterator[psycopg.Connection]:
    """Open a connection acting as an end user (role `authenticated`, auth.uid()
    = user_id), so RLS applies exactly as it would for that user in the app.

    This is also how the AGENT reads (findings §4.1): it borrows the requesting
    member's permissions instead of holding its own. Note that `authenticated`
    has no current_team() — a member can see EVERY team they belong to — so a
    query run in here must carry its own explicit team_id filter.

    Commits on clean exit, rolls back on error. Connects as the dedicated
    authenticator role (comrade_authenticator: LOGIN + noinherit, may only
    SET ROLE authenticated). Raises if COMRADE_AUTHENTICATOR_DB_URL is unset —
    it does NOT fall back to the admin connection, which bypasses RLS. Either
    way, once the role is switched RLS is enforced.
    """
    # No fallback. ADMIN is the table owner and BYPASSRLS, so falling back to
    # it would run every member query with NO row security — silently, and
    # since the pool landed, sharing a pool with the control plane too. A
    # missing variable must stop the process, not quietly disable RLS.
    # (Phase 0 whole-branch review.)
    url = settings.comrade_authenticator_db_url
    if not url:
        raise RuntimeError(
            "COMRADE_AUTHENTICATOR_DB_URL is not set. user_session() will not"
            " fall back to the admin connection, which bypasses RLS."
        )
    with _pool(url).connection() as conn:
        with conn.transaction():
            conn.execute("set local role authenticated")
            conn.execute(
                "select set_config('request.jwt.claims', %s, true)",
                (json.dumps({"sub": str(user_id), "role": "authenticated"}),),
            )
            yield conn
