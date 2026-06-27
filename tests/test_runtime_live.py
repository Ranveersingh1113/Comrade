"""Live integration: a real agent turn records steps and returns a reply.

Requires GEMINI_API_KEY and a reachable DB; skipped without a key. Makes a real
Gemini call.
"""
import pytest

from agent.runtime import run_turn_sync
from shared.agent_runs import get_run
from shared.config import settings
from tests._seed import A1, TEAM_A

pytestmark = pytest.mark.skipif(
    not settings.gemini_api_key, reason="no GEMINI_API_KEY configured"
)


def test_run_turn_records_and_replies(seeded):
    result = run_turn_sync(TEAM_A, A1, "Give me a short status summary.")
    assert result["reply"]
    run = get_run(TEAM_A, result["run_id"])
    assert run["status"] == "done"
    assert run["current_step"] == len(result["steps"])
    # The agent should consult team state before summarising.
    assert any(
        s["type"] == "tool_call" and s["tool"] == "team_get_state"
        for s in result["steps"]
    )
