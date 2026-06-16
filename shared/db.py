"""Direct-Postgres access for the backend workers.

Every worker connects under an RLS-enforced role (never service_role) and scopes
itself to a single team per transaction via SET LOCAL app.current_team_id. The
policies in 0002_rls.sql then constrain every row the worker can see or write.
"""
from contextlib import contextmanager
from enum import Enum
from typing import Iterator

import psycopg

from .config import settings


class Role(str, Enum):
    ADMIN = "admin"        # tests only — table owner, bypasses RLS
    AGENT = "agent"        # conversational agent + platform MCP server
    PIPELINE = "pipeline"  # document parser + memory compiler


_URLS: dict[Role, str] = {
    Role.ADMIN: settings.comrade_db_url_admin,
    Role.AGENT: settings.comrade_agent_db_url,
    Role.PIPELINE: settings.comrade_pipeline_db_url,
}

# how each worker is recorded in change_log (via app.actor_kind GUC)
_ACTOR_KIND: dict[Role, str] = {Role.AGENT: "ai", Role.PIPELINE: "compiler"}


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
