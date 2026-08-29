"""The agent's one kept group-visible write: its reply to an @comrade turn.

Findings §13.1 keeps exactly two group-visible AI writes — the direct reply to
an explicit invocation, and the compiler's diff card. `ag_messages_insert`
restricted the agent to private threads, so the direct reply failed at RLS.
Every HTTP test stubs `_persist_ai_reply`, which is why nothing caught it.
"""
import psycopg
import pytest

from shared.db import Role, team_session
from tests._seed import A1, TEAM_A


def test_agent_can_insert_a_group_reply(seeded):
    with team_session(Role.AGENT, TEAM_A) as conn:
        row = conn.execute(
            "insert into public.messages (team_id, thread_type, thread_owner_id,"
            " sender_kind, body)"
            " values (%s,'group',null,'ai','The demo is Friday.') returning id",
            (TEAM_A,),
        ).fetchone()
    assert row is not None


def test_agent_can_still_insert_a_private_nudge(seeded):
    with team_session(Role.AGENT, TEAM_A) as conn:
        row = conn.execute(
            "insert into public.messages (team_id, thread_type, thread_owner_id,"
            " sender_kind, body)"
            " values (%s,'private',%s,'ai','Checking in.') returning id",
            (TEAM_A, A1),
        ).fetchone()
    assert row is not None


def test_agent_cannot_impersonate_a_member(seeded):
    """The invariant the policy actually protects: never a human's name."""
    with pytest.raises(psycopg.Error):
        with team_session(Role.AGENT, TEAM_A) as conn:
            conn.execute(
                "insert into public.messages (team_id, thread_type,"
                " sender_kind, sender_id, body)"
                " values (%s,'group','user',%s,'not actually me')",
                (TEAM_A, A1),
            )
