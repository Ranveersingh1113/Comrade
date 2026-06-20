"""Direct-Postgres access for the backend workers.

Every worker connects under an RLS-enforced role (never service_role) and scopes
itself to a single team per transaction via SET LOCAL app.current_team_id. The
policies in 0002_rls.sql then constrain every row the worker can see or write.
"""
import json
from contextlib import contextmanager
from enum import Enum
from typing import Iterator

import psycopg

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


@contextmanager
def connect(role: Role) -> Iterator[psycopg.Connection]:
    """Open a raw connection as the given role. Caller manages transactions."""
    conn = psycopg.connect(_URLS[role])
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def team_session(role: Role, team_id: str) -> Iterator[psycopg.Connection]:
    """Open a worker connection scoped to one team for one transaction.

    SET LOCAL app.current_team_id binds current_team() in the RLS policies, so
    every read/write inside the block is confined to `team_id`. The transaction
    commits on clean exit and rolls back on error.
    """
    if role is Role.ADMIN:
        raise ValueError("team_session is for worker roles, not ADMIN")
    conn = psycopg.connect(_URLS[role])
    try:
        with conn.transaction():
            conn.execute(
                "select set_config('app.current_team_id', %s, true)", (str(team_id),)
            )
            conn.execute(
                "select set_config('app.actor_kind', %s, true)", (_ACTOR_KIND[role],)
            )
            yield conn
    finally:
        conn.close()


@contextmanager
def user_session(user_id: str) -> Iterator[psycopg.Connection]:
    """Open a connection acting as an end user (role `authenticated`, auth.uid()
    = user_id), so RLS applies exactly as it would for that user in the app.

    Commits on clean exit, rolls back on error. NOTE: this uses the admin URL as
    a PostgREST-style authenticator that SET ROLEs down to authenticated; once
    the role is switched, RLS is enforced. Production should use a dedicated
    least-privilege authenticator role, not postgres.
    """
    conn = psycopg.connect(_URLS[Role.ADMIN])
    try:
        with conn.transaction():
            conn.execute("set local role authenticated")
            conn.execute(
                "select set_config('request.jwt.claims', %s, true)",
                (json.dumps({"sub": str(user_id), "role": "authenticated"}),),
            )
            yield conn
    finally:
        conn.close()
