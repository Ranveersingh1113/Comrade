"""Live proof that the agent actually reads the wiki (real Gemini)."""
import asyncio

import psycopg
import pytest

from agent.runtime import run_turn
from shared.config import settings
from tests._seed import A1, TEAM_A

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not settings.gemini_api_key, reason="no GEMINI_API_KEY configured"
    ),
]


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def test_agent_answers_from_a_wiki_fact(seeded):
    conn = _admin()
    try:
        page_id = conn.execute(
            "insert into public.memory_pages (team_id, title, description)"
            " values (%s,'Deadlines','key dates') returning id",
            (TEAM_A,),
        ).fetchone()[0]
        entry_id = conn.execute(
            "insert into public.memory_entries (team_id, page_id) values (%s,%s)"
            " returning id",
            (TEAM_A, page_id),
        ).fetchone()[0]
        conn.execute(
            "insert into public.memory_versions (entry_id, team_id, fact, change_type)"
            " values (%s,%s,'The final demo is on 18 December','added')",
            (entry_id, TEAM_A),
        )
    finally:
        conn.close()

    result = asyncio.run(run_turn(TEAM_A, A1, "When is the final demo?"))

    assert "18" in result["reply"] and "december" in result["reply"].lower()
    # it got there by reading the page, not by guessing
    assert any(
        s["type"] == "tool_call" and s["tool"] == "memory_read_page"
        for s in result["steps"]
    )
