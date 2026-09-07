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


def test_steering_reads_run_metadata_without_granting_users_agent_runs(seeded):
    """Run metadata is worker-only; the requester still RLS-reads messages."""
    from agent.history import steering_messages

    thread_id = _thread_id()
    run_id = enqueue_turn(TEAM_A, A1, thread_id, "inspect the failing test")
    active = claim_next_run("worker-one")
    assert active is not None and active.id == run_id
    enqueue_turn(TEAM_A, A1, thread_id, "also check the migration")

    messages = steering_messages(TEAM_A, A1, thread_id, active.id, [])

    assert [body for _, _, body in messages] == ["also check the migration"]


def test_steering_is_injected_before_the_next_model_call(monkeypatch):
    """The runner cannot receive a message during a tool, only before LLM work."""
    from agent.permission_plugin import ChokepointPlugin

    monkeypatch.setattr(
        "agent.permission_plugin.steering_messages",
        lambda *args: [] if args[-1] else [("message-2", None, "also check the migration")],
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
    assert request.contents[0].parts[0].text == (
        "New message from a participant: also^check^the^migration"
    )


def test_a_steering_message_says_who_sent_it(seeded):
    """🔴 It did not. Steering arrived as "New participant message: ...", so in
    a room where anyone can steer, a teammate redirecting the work was
    indistinguishable from the member who asked for it. The model could not
    weigh who was asking, and the run's identity and approval ownership stay
    with the original requester either way — which is exactly why the
    difference has to be visible in the text."""
    from agent.history import steering_messages
    from tests._seed import A2

    thread_id = _thread_id()
    run_id = enqueue_turn(TEAM_A, A1, thread_id, "start the release check")
    active = claim_next_run("worker-one")
    assert active is not None and active.id == run_id
    enqueue_turn(TEAM_A, A2, thread_id, "use the staging branch")

    messages = steering_messages(TEAM_A, A1, thread_id, active.id, [])

    assert len(messages) == 1
    _, sender, body = messages[0]
    assert body == "use the staging branch"
    assert sender, "a team thread attributes its messages"


def test_steering_in_a_private_thread_is_not_attributed(seeded):
    """The same rule recent_turns already applies: a one-participant thread has
    nobody to distinguish, and naming them adds nothing the model can use."""
    from agent.history import steering_messages

    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        row = conn.execute(
            "select t.id from public.threads t join public.thread_participants p"
            " on p.thread_id=t.id where t.team_id=%s and t.visibility='restricted'"
            " and p.user_id=%s",
            (TEAM_A, A1),
        ).fetchone()
    thread_id = str(row[0])
    run_id = enqueue_turn(TEAM_A, A1, thread_id, "draft my update")
    active = claim_next_run("worker-one")
    assert active is not None and active.id == run_id
    enqueue_turn(TEAM_A, A1, thread_id, "shorter")

    messages = steering_messages(TEAM_A, A1, thread_id, active.id, [])

    assert [sender for _, sender, _ in messages] == [None]


def test_the_injected_text_names_the_sender(monkeypatch):
    from agent.permission_plugin import ChokepointPlugin

    monkeypatch.setattr(
        "agent.permission_plugin.steering_messages",
        lambda *args: [] if args[-1] else [("m-2", "Priya", "use staging")],
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

    assert request.contents[0].parts[0].text.startswith("New message from Priya:")
