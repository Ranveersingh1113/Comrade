"""The agent's one kept group-visible write: its reply to an @comrade turn.

Findings §13.1 keeps exactly two group-visible AI writes — the direct reply to
an explicit invocation, and the compiler's diff card. `ag_messages_insert`
restricted the agent to private threads, so the direct reply failed at RLS.
Every HTTP test stubs `_persist_ai_reply`, which is why nothing caught it.
"""
import psycopg
import pytest

from server.app import _persist_ai_reply
from shared.config import settings
from shared.db import Role, team_session
from tests._seed import A1, TEAM_A, count


def test_agent_can_insert_a_group_reply(seeded):
    # no RETURNING: that needs SELECT on messages, revoked by §4.1
    with team_session(Role.AGENT, TEAM_A) as conn:
        cur = conn.execute(
            "insert into public.messages (team_id, thread_type, thread_owner_id,"
            " sender_kind, body)"
            " values (%s,'group',null,'ai','The demo is Friday.')",
            (TEAM_A,),
        )
    assert cur.rowcount == 1


def test_agent_can_still_insert_a_private_nudge(seeded):
    with team_session(Role.AGENT, TEAM_A) as conn:
        cur = conn.execute(
            "insert into public.messages (team_id, thread_type, thread_owner_id,"
            " sender_kind, body)"
            " values (%s,'private',%s,'ai','Checking in.')",
            (TEAM_A, A1),
        )
    assert cur.rowcount == 1


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


def test_persist_ai_reply_returns_the_id_it_wrote(seeded):
    """The real function, unstubbed. §4.1 took SELECT on messages away, so it
    now supplies the id instead of reading it back with RETURNING — and this
    file exists because every HTTP test stubs this function out."""
    message_id = _persist_ai_reply(TEAM_A, "group", None, "The demo is Friday.")

    conn = psycopg.connect(settings.comrade_db_url_admin)
    try:
        assert count(
            conn,
            "select count(*) from public.messages"
            " where id = %s and team_id = %s and sender_kind = 'ai'",
            (message_id, TEAM_A),
        ) == 1
    finally:
        conn.close()
