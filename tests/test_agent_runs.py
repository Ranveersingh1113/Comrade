"""agent_runs observability: durable run + step logging under the AGENT role."""
import psycopg
import pytest

from shared.agent_runs import append_step, finish_run, get_run, start_run
from shared.config import settings
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


def test_append_step_unknown_run_raises(seeded):
    with pytest.raises(LookupError):
        append_step(TEAM_A, "00000000-0000-0000-0000-000000000000", {"seq": 0, "type": "text", "text": "x"})


def test_append_step_out_of_order_seq_comes_back_in_seq_order(seeded):
    """The old jsonb append was implicitly ordered by insertion order. A plain
    insert per step has no such guarantee, so get_run must order by seq."""
    run_id = start_run(TEAM_A, "user", "summary")
    append_step(TEAM_A, run_id, {"seq": 2, "type": "text", "text": "third"})
    append_step(TEAM_A, run_id, {"seq": 0, "type": "text", "text": "first"})
    append_step(TEAM_A, run_id, {"seq": 1, "type": "text", "text": "second"})
    run = get_run(TEAM_A, run_id)
    assert [s["text"] for s in run["steps"]] == ["first", "second", "third"]
    assert run["current_step"] == 3


def test_append_step_no_longer_writes_the_jsonb_column(seeded):
    """§3.2's fix: steps live in agent_steps now, one row per step.
    agent_runs.steps/current_step stay in the schema (dropping is a later,
    easily-reversed-the-other-way migration) but must stop being written."""
    run_id = start_run(TEAM_A, "user", "summary")
    append_step(TEAM_A, run_id, {"seq": 0, "type": "text", "text": "hi"})
    append_step(TEAM_A, run_id, {"seq": 1, "type": "text", "text": "there"})
    admin = psycopg.connect(settings.comrade_db_url_admin)
    try:
        row = admin.execute(
            "select steps, current_step from public.agent_runs where id = %s",
            (run_id,),
        ).fetchone()
    finally:
        admin.close()
    assert row == ([], 0)


def test_append_step_duplicate_seq_raises(seeded):
    """unique(run_id, seq) is what makes a retried append safe rather than
    silently duplicating a step."""
    run_id = start_run(TEAM_A, "user", "summary")
    append_step(TEAM_A, run_id, {"seq": 0, "type": "text", "text": "first"})
    with pytest.raises(psycopg.errors.UniqueViolation):
        append_step(TEAM_A, run_id, {"seq": 0, "type": "text", "text": "dup"})
