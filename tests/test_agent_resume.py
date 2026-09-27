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

    assert claim_effect(TEAM_A, run.id, "team_propose_task", args, worker_id="worker-one") is None
    complete_effect(TEAM_A, run.id, "team_propose_task", args, {"consent_id": "c-1"})

    # The restarted worker re-claims the run, so it is the owner again; the
    # fence is exercised in tests/test_worker_concurrency.py.
    assert claim_effect(
        TEAM_A, run.id, "team_propose_task", args, worker_id="worker-one",
    ) == {"consent_id": "c-1"}
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


def test_resolved_single_action_finishes_without_asking_the_model_again():
    from agent.runtime import _resolved_continuation_reply

    effects = [{
        "tool": "repo_propose_pr",
        "result": {
            "consent_id": "card-1",
            "status": "executed",
            "result": {"pr_url": "https://github.com/acme/repo/pull/7"},
        },
    }]

    assert _resolved_continuation_reply(effects, None) == (
        "Approved action completed: https://github.com/acme/repo/pull/7"
    )
    assert _resolved_continuation_reply([{
        "tool": "team_propose_task",
        "result": {"consent_id": "card-2", "status": "executed"},
    }], None) is None


def test_resumed_tool_call_uses_the_durable_effect_result(monkeypatch):
    from agent.permission_plugin import ChokepointPlugin

    monkeypatch.setattr(
        "agent.permission_plugin.claim_effect", lambda *_, **__: {"consent_id": "c-1"},
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
        claim_effect(TEAM_A, run.id, "team_propose_task", {"title": "Review migration"},
                     worker_id="worker-one")


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
    for status in ("executed", "rejected"):
        assert not _permission_wait({"type": "tool_result", "response": {
            "consent_id": "c-1", "status": status,
        }})


@pytest.mark.parametrize("resolution", ["approve", "edit", "reject"])
def test_resumed_runtime_appends_after_persisted_steps(seeded, monkeypatch, resolution):
    """Resolution resumes safely at the next durable sequence."""
    import asyncio
    from google.adk.events import Event
    from google.genai import types
    from agent.runtime import run_turn
    from agent.run_queue import get_run
    from shared.agent_runs import append_step, pause_for_permission
    from shared.consent import approve_consent, edit_and_approve, propose_action, reject_consent

    thread = _thread_id()
    run_id = enqueue_turn(TEAM_A, A1, thread, "create a task")
    first = claim_next_run("before-approval")
    assert first.id == run_id
    run_id = first.id
    # A gap makes max(seq)+1 distinguishable from counting rows.
    for seq in (0, 4):
        append_step(TEAM_A, run_id, {"seq": seq, "type": "text", "text": "Earlier."},
                    worker_id="before-approval")
    card = propose_action(
        TEAM_A, A1, "task_create",
        {"assignee_id": A1, "title": "Resume checkpoint", "description": None, "deadline": None},
        thread_id=thread, agent_run_id=run_id,
    )["consent_id"]
    args = {"title": "Resume checkpoint"}
    assert claim_effect(TEAM_A, run_id, "team_propose_task", args,
                        worker_id="before-approval") is None
    complete_effect(TEAM_A, run_id, "team_propose_task", args,
                    {"consent_id": card, "status": "pending"})
    pause_for_permission(TEAM_A, run_id, "before-approval")
    if resolution == "approve":
        approve_consent(TEAM_A, card, A1)
    elif resolution == "edit":
        edit_and_approve(TEAM_A, card, A1, {
            "assignee_id": A1, "title": "Edited checkpoint", "description": None, "deadline": None,
        })
    else:
        reject_consent(TEAM_A, card, A1, "No thanks")
    assert claim_next_run("after-approval").id == run_id
    cached = claim_effect(TEAM_A, run_id, "team_propose_task", args,
                          worker_id="after-approval")
    assert cached["status"] == ("rejected" if resolution == "reject" else "executed")
    if resolution == "reject":
        assert cached["resolution_reason"] == "No thanks"

    async def answer(*args, **kwargs):
        if resolution == "reject":
            raise AssertionError("a rejected action must not call the model again")
        yield Event(author="comrade", content=types.Content(
            role="model", parts=[types.Part(text="Continuing remaining work.")]))

    monkeypatch.setattr("agent.runtime.Runner.run_async", answer)
    result = asyncio.run(run_turn(
        TEAM_A, A1, "create a task", thread_id=thread, run_id=run_id,
        worker_id="after-approval", lock_held=True, exclude_message_id=first.input_message_id,
    ))
    assert result["reply"] == (
        "The requested action was rejected."
        if resolution == "reject" else "Continuing remaining work."
    )
    saved = get_run(TEAM_A, run_id)
    assert saved["status"] == "done"
    assert [step["seq"] for step in saved["steps"]] == [0, 4, 5]


def test_retryable_pr_refusal_cannot_be_reclaimed_by_another_worker(seeded):
    run_id = enqueue_turn(TEAM_A, A1, _thread_id(), "propose changes")
    run = claim_next_run("effect-owner")
    assert run.id == run_id
    args = {"title": "Changes", "body": "Check first"}
    refusal = {"error": "Run a check first", "effect_not_started": True}
    assert claim_effect(TEAM_A, run.id, "repo_propose_pr", args,
                        worker_id="effect-owner") is None
    complete_effect(TEAM_A, run.id, "repo_propose_pr", args, refusal)
    assert claim_effect(TEAM_A, run.id, "repo_propose_pr", args,
                        worker_id="wrong-worker") == refusal
    assert completed_effects(TEAM_A, run.id) == [{"tool": "repo_propose_pr", "result": refusal}]
