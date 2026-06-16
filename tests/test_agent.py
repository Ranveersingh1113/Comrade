"""Agent wiring checks (no live model call)."""
from agent.agent import MODEL, root_agent


def test_agent_is_configured():
    assert root_agent.name == "comrade"
    assert root_agent.model == MODEL


def test_team_get_state_tool_registered():
    names = [
        getattr(t, "name", getattr(t, "__name__", "")) for t in root_agent.tools
    ]
    assert "team_get_state" in names
