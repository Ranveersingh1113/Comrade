"""Agent wiring checks (no live model call)."""
from agent.agent import MODEL, root_agent


def test_agent_is_configured():
    assert root_agent.name == "comrade"
    assert root_agent.model == MODEL


def test_tools_registered():
    assert {t.__name__ for t in root_agent.tools} == {
        "team_get_state",
        "memory_read_page",
        "team_propose_task",
        "member_send_nudge",
    }
