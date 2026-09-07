"""A retried send must not post the message twice, or start a second run.

🔴 THE DEFECT. `streamTurn` POSTed a turn with nothing identifying the
attempt. When the POST was accepted and the connection then died — a laptop
lid, a proxy timeout, a deploy mid-request — the browser could not tell
"never arrived" from "arrived and answered". It restored the text to the
composer, the member pressed send again, and the thread got the same question
twice, with two runs and two model bills behind it.

The fix is a client request id that survives the retry, and a uniqueness rule
binding (team, requester, thread, request id) to ONE accepted message.
"""

import psycopg
import pytest

from agent.run_queue import enqueue_turn, get_run
from shared.config import settings
from tests._seed import A1, A2, TEAM_A


def _thread_id() -> str:
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        row = conn.execute(
            "select id from public.threads where team_id=%s and title='General'",
            (TEAM_A,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _messages(thread_id: str, body: str) -> int:
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        return conn.execute(
            "select count(*) from public.messages where thread_id=%s and body=%s",
            (thread_id, body),
        ).fetchone()[0]


def _runs(thread_id: str) -> int:
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        return conn.execute(
            "select count(*) from public.agent_runs where thread_id=%s", (thread_id,)
        ).fetchone()[0]


def test_a_retried_send_posts_one_message_and_one_run(seeded):
    thread = _thread_id()
    before = _runs(thread)

    first = enqueue_turn(TEAM_A, A1, thread, "ship it?", client_request_id="req-1")
    second = enqueue_turn(TEAM_A, A1, thread, "ship it?", client_request_id="req-1")

    assert str(second) == str(first), "the retry must resolve to the accepted run"
    assert second.status == "duplicate"
    assert _messages(thread, "ship it?") == 1
    assert _runs(thread) == before + 1


def test_a_retry_returns_the_run_so_the_browser_can_follow_it(seeded):
    """A duplicate is not an error: the turn was accepted and is answerable."""
    thread = _thread_id()
    run_id = enqueue_turn(TEAM_A, A1, thread, "who is on call?", client_request_id="req-2")
    again = enqueue_turn(TEAM_A, A1, thread, "who is on call?", client_request_id="req-2")

    run = get_run(TEAM_A, str(again))
    assert run is not None and run["id"] == str(run_id)


def test_a_send_with_no_request_id_is_never_deduplicated(seeded):
    """Two people asking the same question is not a duplicate submission."""
    thread = _thread_id()
    enqueue_turn(TEAM_A, A1, thread, "standup time?")
    enqueue_turn(TEAM_A, A1, thread, "standup time?")

    assert _messages(thread, "standup time?") == 2


def test_the_request_id_is_scoped_to_its_sender(seeded):
    """Otherwise one member's id collides with another's and swallows a message."""
    thread = _thread_id()
    enqueue_turn(TEAM_A, A1, thread, "from a1", client_request_id="shared")
    enqueue_turn(TEAM_A, A2, thread, "from a2", client_request_id="shared")

    assert _messages(thread, "from a1") == 1
    assert _messages(thread, "from a2") == 1


def test_a_steering_message_is_deduplicated_too(seeded):
    """The retry problem is identical while a run is active — and there the
    second copy is not merely noise, it is a second instruction to the model."""
    thread = _thread_id()
    first = enqueue_turn(TEAM_A, A1, thread, "start", client_request_id="s-0")
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        conn.execute(
            "update public.agent_runs set status='running', worker_id='w',"
            " lease_expires_at=now()+interval '5 minutes' where id=%s",
            (str(first),),
        )

    steer = enqueue_turn(TEAM_A, A1, thread, "actually, wait", client_request_id="s-1")
    again = enqueue_turn(TEAM_A, A1, thread, "actually, wait", client_request_id="s-1")

    assert steer.status == "steering"
    assert again.status == "duplicate"
    assert _messages(thread, "actually, wait") == 1


@pytest.mark.parametrize("body", ["concurrent"])
def test_a_concurrent_double_submit_cannot_slip_between_check_and_insert(seeded, body):
    """The lookup alone is check-then-act. The unique index is what holds."""
    thread = _thread_id()
    enqueue_turn(TEAM_A, A1, thread, body, client_request_id="race")

    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "insert into public.messages (team_id, thread_id, sender_kind,"
                " sender_id, body, client_request_id)"
                " values (%s, %s, 'user', %s, %s, 'race')",
                (TEAM_A, thread, A1, body),
            )


def test_a_retry_does_not_spend_the_hourly_budget_twice(seeded, monkeypatch):
    """A flaky connection must not be able to eat a team's turn allowance.

    The budget is reserved BEFORE the turn is persisted (that ordering is what
    stops two concurrent turns both passing the cap). So a retry reserves
    again, and unless the duplicate hands it back, every uncertain send costs
    a turn that never reached the model."""
    from fastapi.testclient import TestClient

    from server.app import app
    from server.auth import current_user_id
    from shared.config import settings

    thread = _thread_id()
    monkeypatch.setattr(settings, "agent_turns_per_hour", 2)
    app.dependency_overrides[current_user_id] = lambda: A1
    try:
        client = TestClient(app)
        body = {
            "team_id": TEAM_A, "thread_id": thread, "text": "deploy?",
            "client_request_id": "budget-1",
        }
        first = client.post("/agent/turn", json=body)
        again = client.post("/agent/turn", json=body)
        # A third, distinct turn still fits: the retry gave its reservation back.
        third = client.post("/agent/turn", json={
            "team_id": TEAM_A, "thread_id": thread, "text": "and now?",
            "client_request_id": "budget-2",
        })
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 200, first.text
    assert again.status_code == 200, again.text
    assert again.json()["run_id"] == first.json()["run_id"]
    assert again.json()["status"] == "duplicate"
    assert third.status_code == 200, third.text


def test_the_previous_signature_still_works_during_a_release(seeded):
    """Releases build, then migrate, then activate. Between the migration and
    the new image the RUNNING api still calls the four-argument form, so
    dropping it would turn that window into an outage."""
    thread = _thread_id()
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        conn.execute(
            "select set_config('request.jwt.claims', %s, true)",
            ('{"sub": "%s"}' % A1,),
        )
        row = conn.execute(
            "select run_id, disposition from public.enqueue_agent_turn(%s, %s, %s, %s)",
            (TEAM_A, thread, "old client", "user"),
        ).fetchone()
        conn.rollback()

    assert row is not None and row[1] in {"queued", "steering"}
