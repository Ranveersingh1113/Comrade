"""agent_runs observability: durable run + step logging under the AGENT role."""
from shared.agent_runs import append_step, finish_run, get_run, start_run
from tests._seed import TEAM_A, TEAM_B


def test_start_run_creates_running_row(seeded):
    run_id = start_run(TEAM_A, "user", "give me a status summary")
    run = get_run(TEAM_A, run_id)
    assert run is not None
    assert run["status"] == "running"
    assert run["current_step"] == 0
    assert run["steps"] == []
    assert run["trigger_type"] == "user"


def test_append_step_orders_and_counts(seeded):
    run_id = start_run(TEAM_A, "user", "summary")
    append_step(TEAM_A, run_id, {"seq": 0, "type": "tool_call", "tool": "team_get_state"})
    append_step(TEAM_A, run_id, {"seq": 1, "type": "text", "text": "All caught up."})
    run = get_run(TEAM_A, run_id)
    assert run["current_step"] == 2
    assert [s["type"] for s in run["steps"]] == ["tool_call", "text"]


def test_finish_run_sets_terminal_status(seeded):
    run_id = start_run(TEAM_A, "user", "summary")
    finish_run(TEAM_A, run_id, "done")
    run = get_run(TEAM_A, run_id)
    assert run["status"] == "done"
    assert run["finished_at"] is not None


def test_runs_are_team_scoped(seeded):
    run_id = start_run(TEAM_A, "user", "summary")
    # TEAM_B's agent session must not see TEAM_A's run (RLS via current_team()).
    assert get_run(TEAM_B, run_id) is None
