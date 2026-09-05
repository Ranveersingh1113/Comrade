"""Effects from a reclaimed run must never be executed twice."""

import psycopg
from types import SimpleNamespace

from agent.effects import complete_effect, claim_effect
from agent.run_queue import claim_next_run, enqueue_turn
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
