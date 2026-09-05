"""Live proof that the agent actually reads the room (real Gemini).

The fact below is planted in the GROUP room and nowhere else: the wiki is
empty in this fixture, team state has no such task, and the turn runs in A1's
PRIVATE thread — so history replay (which is thread-scoped) cannot carry it
either. messages_search is the only route to the answer.
"""
import asyncio

import psycopg
import pytest

from agent.runtime import run_turn
from shared.config import settings
from tests._seed import A2, TEAM_A, A1

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not settings.gemini_api_key, reason="no GEMINI_API_KEY configured"
    ),
]


def _thread_id() -> str:
    conn = psycopg.connect(settings.comrade_db_url_admin)
    try:
        row = conn.execute(
            "select id from public.threads where team_id=%s"
            " and legacy_thread_owner_id=%s",
            (TEAM_A, A1),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0])


def test_agent_answers_from_something_said_in_the_room(seeded):
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        conn.execute(
            "insert into public.messages (team_id, thread_type, sender_kind,"
            " sender_id, body) values (%s,'group','user',%s,%s)",
            (TEAM_A, A2, "I booked room B-114 in the library annexe for our"
                         " demo rehearsal."),
        )
    finally:
        conn.close()

    result = asyncio.run(
        run_turn(
            TEAM_A, A1, "Which room did we book for the demo rehearsal?",
            thread_id=_thread_id(),
        )
    )

    assert "B-114" in result["reply"]
    # it got there by searching the room, not by guessing
    assert any(
        s["type"] == "tool_call" and s["tool"] == "messages_search"
        for s in result["steps"]
    )
