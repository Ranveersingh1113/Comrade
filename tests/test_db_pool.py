"""Connections come from a pool, not one per operation (findings §3.2).

The rule is "never open a connection per request"; the codebase opened one per
operation. `append_step` was the worst case — 20 connections for a 20-step
turn. These tests pin the reuse, and — more importantly — pin the safety
property that makes reuse legal: every scoping statement is transaction-scoped,
so a recycled connection cannot hand the next borrower the previous borrower's
team or identity.

Note on what is NOT asserted: a pool gives no guarantee about WHICH backend a
given borrow gets. Connections are returned asynchronously and the pool may
hold several, so "the same pid twice in a row" is a scheduling coincidence, not
a contract. These tests assert that N borrows do not cost N backends, which is
the property that actually matters.
"""
from shared.db import Role, _pool, connect, team_session, user_session
from shared.config import settings
from tests._seed import A1, A2, TEAM_A, TEAM_B

_BORROWS = 20


def _backend_pid(conn):
    return conn.execute("select pg_backend_pid()").fetchone()[0]


def test_worker_sessions_do_not_cost_a_backend_each(seeded):
    pids = set()
    for _ in range(_BORROWS):
        with team_session(Role.AGENT, TEAM_A) as conn:
            pids.add(_backend_pid(conn))
    assert len(pids) < _BORROWS, "every borrow opened its own backend — not pooled"


def test_user_sessions_do_not_cost_a_backend_each(seeded):
    pids = set()
    for _ in range(_BORROWS):
        with user_session(A1) as conn:
            pids.add(_backend_pid(conn))
    assert len(pids) < _BORROWS, "every borrow opened its own backend — not pooled"


def test_team_scope_is_re_established_on_every_borrow(seeded):
    for _ in range(3):
        with team_session(Role.AGENT, TEAM_A) as conn:
            assert conn.execute(
                "select current_setting('app.current_team_id', true)"
            ).fetchone()[0] == TEAM_A


def test_user_sessions_always_act_as_authenticated(seeded):
    for _ in range(3):
        with user_session(A1) as conn:
            assert conn.execute("select current_user").fetchone()[0] == "authenticated"


# ---------------------------------------------------------------------------
# The cross-tenant guards. These are the tests that catch a scoping leak.
# ---------------------------------------------------------------------------

def test_a_recycled_connection_never_inherits_the_previous_team(seeded):
    """Alternate teams across many borrows; the scope must follow the borrower.

    If a future edit made the scoping session-scoped (`SET` instead of
    `SET LOCAL`, or set_config(..., false)) this goes red — and that would be a
    cross-tenant leak, not a cosmetic one.
    """
    pids = set()
    for i in range(_BORROWS):
        want = TEAM_A if i % 2 == 0 else TEAM_B
        with team_session(Role.AGENT, want) as conn:
            pids.add(_backend_pid(conn))
            assert conn.execute(
                "select current_setting('app.current_team_id', true)"
            ).fetchone()[0] == want
    assert len(pids) < _BORROWS, "no connection was reused — the guard proved nothing"


def test_a_recycled_user_connection_never_inherits_the_previous_member(seeded):
    """The identity half of the same guard: auth.uid() must follow the borrower."""
    pids = set()
    for i in range(_BORROWS):
        want = A1 if i % 2 == 0 else A2
        with user_session(want) as conn:
            pids.add(_backend_pid(conn))
            assert str(conn.execute("select auth.uid()").fetchone()[0]) == want
    assert len(pids) < _BORROWS, "no connection was reused — the guard proved nothing"


def test_connect_still_supports_autocommit_for_the_worker(seeded):
    """pipeline/worker.py borrows via connect() and sets autocommit itself.

    The queue's claim/finish statements must land immediately and outside any
    enclosing transaction, so this usage has to survive pooling — and must not
    leave the next borrower stuck in autocommit.
    """
    with connect(Role.ADMIN) as conn:
        conn.autocommit = True
        conn.execute(
            "insert into public.jobs (team_id, job_type)"
            " values (%s,'parse_document')",
            (TEAM_A,),
        )
    for _ in range(_BORROWS):
        with connect(Role.ADMIN) as conn:
            assert conn.autocommit is False, "autocommit leaked to the next borrower"
    with connect(Role.ADMIN) as conn:
        assert conn.execute(
            "select count(*) from public.jobs where team_id=%s", (TEAM_A,)
        ).fetchone()[0] >= 1


def test_a_bare_borrow_after_a_scoped_one_is_clean(seeded):
    """The guard that actually catches session-scoped scoping.

    Alternating scoped borrows cannot catch it — each one overwrites the
    setting with the right value, so a leak stays invisible. The leak is only
    observable from a borrow that sets NO scope of its own: `connect()` shares
    a pool with `team_session()` for the same role, so if
    `app.current_team_id` were session-scoped it would still be TEAM_A here.
    """
    with team_session(Role.AGENT, TEAM_A) as conn:
        assert conn.execute(
            "select current_setting('app.current_team_id', true)"
        ).fetchone()[0] == TEAM_A

    for _ in range(_BORROWS):
        with connect(Role.AGENT) as conn:
            leaked = conn.execute(
                "select current_setting('app.current_team_id', true)"
            ).fetchone()[0]
            assert not leaked, f"team scope leaked to a bare borrow: {leaked!r}"


def test_a_bare_borrow_after_a_user_session_is_not_authenticated(seeded):
    """Same guard for identity: `set local role` must not outlive its transaction."""
    url = settings.comrade_authenticator_db_url or settings.comrade_db_url_admin
    with user_session(A1) as conn:
        assert conn.execute("select current_user").fetchone()[0] == "authenticated"

    for _ in range(_BORROWS):
        with _pool(url).connection() as conn:
            who = conn.execute("select current_user").fetchone()[0]
            assert who != "authenticated", "SET ROLE leaked to a bare borrow"
            claims = conn.execute(
                "select current_setting('request.jwt.claims', true)"
            ).fetchone()[0]
            assert not claims, f"jwt claims leaked to a bare borrow: {claims!r}"
