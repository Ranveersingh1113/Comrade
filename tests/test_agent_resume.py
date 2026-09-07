"""Effects from a reclaimed run must never be executed twice."""

import psycopg
import pytest
from types import SimpleNamespace

from agent.effects import RunInactive, complete_effect, claim_effect, completed_effects
from agent.run_queue import cancel_run, claim_next_run, enqueue_turn
from shared.config import settings
from tests._seed import A1, TEAM_A


def _thread_id() -> str:
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        row = conn.execute(
            "select id from public.threads where team_id=%s and title='General'",
            (TEAM_A,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_completed_effect_returns_its_saved_result_after_worker_restart(seeded):
    run_id = enqueue_turn(TEAM_A, A1, _thread_id(), "create the task")
    run = claim_next_run("worker-one")
    assert run is not None and run.id == run_id
    args = {"title": "Review migration"}

    assert claim_effect(TEAM_A, run.id, "team_propose_task", args) is None
    complete_effect(TEAM_A, run.id, "team_propose_task", args, {"consent_id": "c-1"})

    assert claim_effect(TEAM_A, run.id, "team_propose_task", args) == {"consent_id": "c-1"}
    assert completed_effects(TEAM_A, run.id) == [
        {"tool": "team_propose_task", "result": {"consent_id": "c-1"}}
    ]


def test_continuation_record_marks_completed_effect_data():
    from agent.runtime import _continuation_content

    content = _continuation_content([
        {"tool": "team_propose_task", "result": {"consent_id": "c-1"}},
    ])

    assert content is not None
    assert content.parts[0].text.startswith("Continuation record (data): ")
    assert "team_propose_task" in content.parts[0].text
    assert "consent_id" in content.parts[0].text


def test_resumed_tool_call_uses_the_durable_effect_result(monkeypatch):
    from agent.permission_plugin import ChokepointPlugin

    monkeypatch.setattr(
        "agent.permission_plugin.claim_effect", lambda *_: {"consent_id": "c-1"},
    )
    import asyncio
    result = asyncio.run(ChokepointPlugin().before_tool_callback(
        tool=SimpleNamespace(name="team_propose_task"), tool_args={"title": "Review migration"},
        tool_context=SimpleNamespace(state={"team_id": TEAM_A, "agent_run_id": "run-1"}),
    ))

    assert result == {"consent_id": "c-1"}


def test_cancelled_run_cannot_begin_another_effect(seeded):
    run_id = enqueue_turn(TEAM_A, A1, _thread_id(), "create the task")
    run = claim_next_run("worker-one")
    assert run is not None and run.id == run_id
    assert cancel_run(TEAM_A, run.id, requester_id=A1)

    with pytest.raises(RunInactive):
        claim_effect(TEAM_A, run.id, "team_propose_task", {"title": "Review migration"})


def test_approval_requeues_its_waiting_agent_run(seeded):
    from shared.consent import approve_consent, propose_action

    thread_id = _thread_id()
    run_id = enqueue_turn(TEAM_A, A1, thread_id, "create a task")
    run = claim_next_run("worker-one")
    assert run is not None and run.id == run_id
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        conn.execute("update public.agent_runs set status='waiting_for_permission' where id=%s", (run.id,))
        conn.commit()
    consent_id = propose_action(
        TEAM_A, A1, "task_create",
        {"assignee_id": A1, "title": "approved later", "description": None, "deadline": None},
        thread_id=thread_id, agent_run_id=run.id,
    )["consent_id"]

    assert approve_consent(TEAM_A, consent_id, A1)["status"] == "executed"
    assert claim_next_run("worker-two").id == run.id


def test_consent_tool_result_pauses_the_run():
    from agent.runtime import _permission_wait

    assert _permission_wait({"type": "tool_result", "response": {"consent_id": "c-1"}})
    assert not _permission_wait({"type": "tool_result", "response": {"task_id": "t-1"}})
