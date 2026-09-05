"""The agent's one kept group-visible write: its reply to an @comrade turn.

Findings §13.1 keeps exactly two group-visible AI writes — the direct reply to
an explicit invocation, and the compiler's diff card. `ag_messages_insert`
restricted the agent to private threads, so the direct reply failed at RLS.
Every HTTP test stubs `_persist_ai_reply`, which is why nothing caught it.
"""
import psycopg
import pytest

from server.app import _persist_ai_reply, _resolve_thread
from shared.config import settings
from shared.db import Role, team_session
from tests._seed import A1, TEAM_A, count, general_thread, personal_thread


def test_agent_can_insert_a_group_reply(seeded):
    # no RETURNING: that needs SELECT on messages, revoked by §4.1
    with team_session(Role.AGENT, TEAM_A) as conn:
        cur = conn.execute(
            "insert into public.messages (team_id, thread_id,"
            " sender_kind, body)"
            " values (%s,%s,'ai','The demo is Friday.')",
            (TEAM_A, general_thread(conn, TEAM_A)),
        )
    assert cur.rowcount == 1


def test_agent_can_still_insert_a_private_nudge(seeded):
    with team_session(Role.AGENT, TEAM_A) as conn:
        cur = conn.execute(
            "insert into public.messages (team_id, thread_id,"
            " sender_kind, body)"
            " values (%s,%s,'ai','Checking in.')",
            (TEAM_A, personal_thread(conn, TEAM_A, A1)),
        )
    assert cur.rowcount == 1


def test_agent_cannot_impersonate_a_member(seeded):
    """The invariant the policy actually protects: never a human's name."""
    with team_session(Role.AGENT, TEAM_A) as conn:
        # Resolved BEFORE the raises block, and deliberately. If the thread
        # lookup were inside it, a missing thread would raise psycopg.Error
        # too and this test would pass without ever reaching the policy it
        # exists to check.
        thread_id = general_thread(conn, TEAM_A)
        with pytest.raises(psycopg.Error):
            conn.execute(
                "insert into public.messages (team_id, thread_id,"
                " sender_kind, sender_id, body)"
                " values (%s,%s,'user',%s,'not actually me')",
                (TEAM_A, thread_id, A1),
            )


def test_persist_ai_reply_returns_the_id_it_wrote(seeded):
    """The real function, unstubbed. §4.1 took SELECT on messages away, so it
    now supplies the id instead of reading it back with RETURNING — and this
    file exists because every HTTP test stubs this function out."""
    conn = psycopg.connect(settings.comrade_db_url_admin)
    try:
        # _resolve_thread takes a canonical thread id now and returns one; the
        # old (user, team, owner, thread_type) form went with the contract
        # migration. It still runs as the requesting user, so this also checks
        # A1 may see the thread at all.
        thread_id = _resolve_thread(A1, TEAM_A, general_thread(conn, TEAM_A))
        message_id = _persist_ai_reply(TEAM_A, thread_id, "The demo is Friday.")
        assert count(
            conn,
            "select count(*) from public.messages"
            " where id = %s and team_id = %s and sender_kind = 'ai'",
            (message_id, TEAM_A),
        ) == 1
    finally:
        conn.close()
