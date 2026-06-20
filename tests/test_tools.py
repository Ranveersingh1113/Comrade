"""Tests for ADK platform tools (plain functions, no LLM)."""
import psycopg

from agent.tools import fetch_team_state
from shared.config import settings
from tests._seed import A1, A2, TEAM_A, TEAM_B


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def test_team_get_state_returns_full_snapshot(seeded):
    # add a live task and a pending consent item (seed has neither)
    conn = _admin()
    try:
        conn.execute(
            "insert into public.tasks (team_id, assignee_id, title, status,"
            " created_by_kind, created_by_id) values"
            " (%s, %s, 'Build UI', 'proposed', 'user', %s)",
            (TEAM_A, A2, A1),
        )
        conn.execute(
            "insert into public.consent_queue (team_id, requesting_member_id,"
            " tool_name, tool_args, action_hash) values"
            " (%s, %s, 'team_post_message', '{}', 'h1')",
            (TEAM_A, A1),
        )
    finally:
        conn.close()

    state = fetch_team_state(TEAM_A)

    assert state["team"]["name"] == "Team A"
    assert {m["display_name"] for m in state["members"]} == {"A1", "A2"}
    assert any(m["role"] == "leader" for m in state["members"])
    assert any(t["title"] == "Build UI" for t in state["tasks"])
    assert any(c["tool_name"] == "team_post_message" for c in state["open_consent"])


def test_team_get_state_scopes_to_the_requested_team(seeded):
    # team B exists but is a different team; A's snapshot must not include it
    state_a = fetch_team_state(TEAM_A)
    assert state_a["team"]["id"] == TEAM_A
    # B's members (B1/B2) must not leak into A's snapshot
    assert {m["display_name"] for m in state_a["members"]}.isdisjoint({"B1", "B2"})

    state_b = fetch_team_state(TEAM_B)
    assert state_b["team"]["name"] == "Team B"
