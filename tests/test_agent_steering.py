"""A new agent message steers the active turn instead of failing its queue."""

import psycopg
from types import SimpleNamespace

from agent.run_queue import claim_next_run, enqueue_turn, get_run
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


def test_message_during_active_run_is_durable_steering(seeded):
    thread_id = _thread_id()
    first = enqueue_turn(TEAM_A, A1, thread_id, "inspect the failing test")
    active = claim_next_run("worker-one")
    assert active is not None and active.id == first

    steering = enqueue_turn(TEAM_A, A1, thread_id, "also check the migration")

    assert steering == active.id
    assert steering.status == "steering"
    assert get_run(TEAM_A, active.id)["status"] == "running"
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        bodies = conn.execute(
            "select body from public.messages where thread_id=%s order by created_at, id",
            (thread_id,),
        ).fetchall()
    assert [body for (body,) in bodies][-2:] == [
        "inspect the failing test", "also check the migration"
    ]


def test_steering_is_injected_before_the_next_model_call(monkeypatch):
    """The runner cannot receive a message during a tool, only before LLM work."""
    from agent.permission_plugin import ChokepointPlugin

    monkeypatch.setattr(
        "agent.permission_plugin.steering_messages",
        lambda *args: [] if args[-1] else [("message-2", "also check the migration")],
    )
    request = SimpleNamespace(contents=[])
    context = SimpleNamespace(state={
        "agent_run_id": "run-1", "team_id": TEAM_A, "requester_id": A1,
        "thread_id": "thread-1", "steering_message_ids": [],
    })

    import asyncio
    asyncio.run(ChokepointPlugin().before_model_callback(
        callback_context=context, llm_request=request,
    ))
    asyncio.run(ChokepointPlugin().before_model_callback(
        callback_context=context, llm_request=request,
    ))

    assert context.state["steering_message_ids"] == ["message-2"]
    assert len(request.contents) == 1
    assert request.contents[0].parts[0].text == "New participant message: also^check^the^migration"
