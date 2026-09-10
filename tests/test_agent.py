"""Agent wiring checks (no live model call)."""
from agent.agent import MODEL, root_agent


def test_agent_is_configured():
    assert root_agent.name == "comrade"
    assert root_agent.model == MODEL


def test_tools_registered():
    assert {t.__name__ for t in root_agent.tools} == {
        "team_get_state",
        "memory_read_page",
        "memory_search",
        "repo_read",
        "repo_glob",
        "repo_grep",
        "repo_edit",
        "repo_run",
        "process_start",
        "process_logs",
        "process_stop",
        "repo_propose_pr",
        "repo_activity",
        "member_activity",
        "messages_search",
        "document_read",
        "now",
        "task_get",
        "team_propose_task",
        "task_propose_update",
        "member_send_nudge",
        "plan_update",
    }


def test_agent_uses_an_explicit_thinking_budget():
    # F52: Gemini dynamic thinking returned STOP with zero output on real
    # judge prompts. Keep reasoning enabled with an explicit budget.
    config = root_agent.generate_content_config
    assert config is not None
    assert config.thinking_config.thinking_budget == 1024
