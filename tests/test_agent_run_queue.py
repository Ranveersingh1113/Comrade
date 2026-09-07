"""Durable ordering and recovery for agent turns."""

import psycopg

from agent.run_queue import (
    MAX_ATTEMPTS, cancel_run, claim_next_run, enqueue_turn, finish_claimed_run,
    get_run, input_for, recover_expired_runs,
)
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


def _private_thread_id() -> str:
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        row = conn.execute(
            "select t.id from public.threads t join public.thread_participants p"
            " on p.thread_id=t.id where t.team_id=%s and t.visibility='restricted'"
            " and p.user_id=%s",
            (TEAM_A, A1),
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_enqueued_turn_is_durable_before_a_worker_claims_it(seeded):
    """Removing the queue insert would lose a submitted turn on worker crash."""
    run_id = enqueue_turn(TEAM_A, A1, _thread_id(), "status?")

    run = get_run(TEAM_A, run_id)

    assert run["status"] == "queued"
    assert run["attempts"] == 0
    claimed = claim_next_run("worker")
    assert claimed is not None and claimed.id == run_id
    assert input_for(claimed).input_text == "status?"


def test_same_thread_runs_claim_in_order(seeded):
    first = enqueue_turn(TEAM_A, A1, _thread_id(), "first")
    second = enqueue_turn(TEAM_A, A1, _thread_id(), "second")

    claimed = claim_next_run("worker-one")

    assert claimed is not None and claimed.id == first
    assert claim_next_run("worker-two") is None
    assert finish_claimed_run(claimed, "done")
    claimed = claim_next_run("worker-two")
    assert claimed is not None and claimed.id == second


def test_different_threads_claim_concurrently(seeded):
    general = enqueue_turn(TEAM_A, A1, _thread_id(), "general")
    private = enqueue_turn(TEAM_A, A1, _private_thread_id(), "private")

    one = claim_next_run("worker-one")
    two = claim_next_run("worker-two")

    assert {one.id, two.id} == {general, private}


def test_expired_run_is_reclaimed(seeded):
    run_id = enqueue_turn(TEAM_A, A1, _thread_id(), "retry me")
    first = claim_next_run("worker-one")
    assert first is not None
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        conn.execute(
            "update public.agent_runs set lease_expires_at=now()-interval '1 second' where id=%s",
            (run_id,),
        )
        conn.commit()

    reclaimed = claim_next_run("worker-two")

    assert reclaimed is not None and reclaimed.id == run_id
    assert reclaimed.attempts == 2


def test_final_expired_lease_fails_instead_of_retrying(seeded):
    run_id = enqueue_turn(TEAM_A, A1, _thread_id(), "last chance")
    claimed = claim_next_run("worker-one")
    assert claimed is not None
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        conn.execute(
            "update public.agent_runs set attempts=%s, lease_expires_at=now()-interval '1 second' where id=%s",
            (MAX_ATTEMPTS, run_id),
        )
        conn.commit()

    assert recover_expired_runs() == 1
    assert get_run(TEAM_A, run_id)["status"] == "failed"
    assert claim_next_run("worker-two") is None


def test_cancellation_prevents_claiming_or_more_work(seeded):
    run_id = enqueue_turn(TEAM_A, A1, _thread_id(), "stop")

    # requester_id is required, not defaulted: ownership is enforced in the
    # same statement as the write, so there is no fail-open shape to pass.
    assert cancel_run(TEAM_A, run_id, requester_id=A1)
    assert claim_next_run("worker") is None
    assert get_run(TEAM_A, run_id)["status"] == "cancelled"
