"""Two teams working at once, and a worker that has lost its claim.

🔴 THE DEFECTS.

The agent worker was strictly serial: `main()` ran one turn to completion
before claiming the next. `claim_next_agent_run` already guarantees one active
run per THREAD, so nothing about the queue required this — but in practice one
long turn anywhere in the deployment made every other team wait behind it.

And a worker that lost its lease kept going. `renew_lease` returning False
ended the renewer thread and told nobody; `claim_effect` fenced on the run
being `running`, which says nothing about WHO is running it. So after recovery
handed a run to a second worker, the first could still perform that run's
effects — the same external action done twice, by two processes that each
believed they owned the job.
"""
import psycopg
import pytest

from agent.effects import RunInactive, claim_effect
from agent.run_queue import claim_next_run, enqueue_turn, recover_expired_runs
from shared.config import settings
from tests._seed import A1, A2, TEAM_A, TEAM_B, B1


def _thread(team_id: str, title: str = "General") -> str:
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        row = conn.execute(
            "select id from public.threads where team_id=%s and title=%s",
            (team_id, title),
        ).fetchone()
    assert row is not None, f"no {title!r} thread in {team_id}"
    return str(row[0])


def _new_thread(team_id: str, title: str) -> str:
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        row = conn.execute(
            "insert into public.threads (team_id, title, visibility, kind, created_by)"
            " values (%s,%s,'team','discussion',%s) returning id",
            (team_id, title, A1),
        ).fetchone()
        conn.commit()
    return str(row[0])


def _expire_lease(run_id: str) -> None:
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        conn.execute(
            "update public.agent_runs set lease_expires_at = now() - interval '1 hour'"
            " where id=%s",
            (run_id,),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Fencing: a stale worker must not finish another worker's reclaimed job
# ---------------------------------------------------------------------------

def test_a_stale_worker_cannot_perform_an_effect_for_a_reclaimed_run(seeded):
    """The first worker did not crash — it stalled, and its lease ran out.

    Recovery gave the run to somebody else. The first process is still alive
    and still holds the tool call it was about to make, and `status='running'`
    is true again — for the OTHER worker. Fencing on the status alone let it
    through, and the effect happened twice."""
    thread = _thread(TEAM_A)
    run_id = str(enqueue_turn(TEAM_A, A1, thread, "open the pull request"))
    first = claim_next_run("worker-one")
    assert first is not None and first.id == run_id

    _expire_lease(run_id)
    recover_expired_runs()
    second = claim_next_run("worker-two")
    assert second is not None and second.id == run_id, "recovery must requeue it"

    with pytest.raises(RunInactive):
        claim_effect(
            TEAM_A, run_id, "repo_open_pr", {"title": "x"}, worker_id="worker-one",
        )


def test_the_worker_that_holds_the_claim_can_still_act(seeded):
    """The fence must not stop the process actually doing the work."""
    thread = _thread(TEAM_A)
    run_id = str(enqueue_turn(TEAM_A, A1, thread, "open the pull request"))
    run = claim_next_run("worker-one")
    assert run is not None

    assert claim_effect(
        TEAM_A, run_id, "repo_open_pr", {"title": "x"}, worker_id="worker-one",
    ) is None


def test_a_run_nobody_leased_is_not_fenced_against_a_worker(seeded):
    """An inline run has no owner, so there is nothing to be stale against.

    `is not distinct from` rather than `=`: a NULL owner matches a NULL
    caller and nothing else, so this stays a fence rather than a hole."""
    thread = _thread(TEAM_A)
    run_id = str(enqueue_turn(TEAM_A, A1, thread, "inline"))
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        conn.execute(
            "update public.agent_runs set status='running' where id=%s", (run_id,)
        )
        conn.commit()

    assert claim_effect(
        TEAM_A, run_id, "task_create", {"title": "x"}, worker_id=None,
    ) is None
    with pytest.raises(RunInactive):
        claim_effect(
            TEAM_A, run_id, "task_update", {"task_id": "y"}, worker_id="worker-one",
        )


# ---------------------------------------------------------------------------
# Concurrency: independent threads progress together, one turn per thread
# ---------------------------------------------------------------------------

def test_two_threads_can_run_at_the_same_time(seeded):
    """🔴 They could not. One long turn made every other team wait behind it,
    though nothing in the queue required that."""
    first_thread = _thread(TEAM_A)
    second_thread = _new_thread(TEAM_A, "Release")
    enqueue_turn(TEAM_A, A1, first_thread, "read the repository")
    enqueue_turn(TEAM_A, A1, second_thread, "check the migration")

    one = claim_next_run("worker-one")
    two = claim_next_run("worker-two")

    assert one is not None and two is not None
    assert one.id != two.id
    assert one.thread_id != two.thread_id


def test_one_thread_still_runs_one_turn_at_a_time(seeded):
    """Ordering within a thread is the thing concurrency must not cost."""
    thread = _thread(TEAM_A)
    enqueue_turn(TEAM_A, A1, thread, "first")
    claimed = claim_next_run("worker-one")
    assert claimed is not None
    # A second message while a run is active is steering, not a new run, so
    # queue a second run the way a scheduled trigger would.
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        conn.execute(
            "insert into public.agent_runs (team_id, requester_id, thread_id,"
            " input_message_id, trigger_type, input_summary, status)"
            " select team_id, requester_id, thread_id, input_message_id,"
            " 'scheduled', 'second', 'queued' from public.agent_runs where id=%s",
            (claimed.id,),
        )
        conn.commit()

    assert claim_next_run("worker-two") is None


def test_one_team_cannot_occupy_every_worker(seeded, monkeypatch):
    """🔴 Concurrency without a per-team ceiling is just a bigger blast radius:
    one team queueing five turns takes every slot and everyone else waits."""
    monkeypatch.setattr(settings, "comrade_agent_max_running_per_team", 1)
    busy = _thread(TEAM_A)
    other = _new_thread(TEAM_A, "Also busy")
    enqueue_turn(TEAM_A, A1, busy, "one")
    enqueue_turn(TEAM_A, A2, other, "two")

    first = claim_next_run("worker-one")
    second = claim_next_run("worker-two")

    assert first is not None
    assert second is None, "the team's second turn waits for its own slot"


def test_the_ceiling_is_per_team_not_global(seeded, monkeypatch):
    """Another team's queue must not be blocked by a busy neighbour."""
    monkeypatch.setattr(settings, "comrade_agent_max_running_per_team", 1)
    enqueue_turn(TEAM_A, A1, _thread(TEAM_A), "theirs")
    enqueue_turn(TEAM_B, B1, _thread(TEAM_B), "ours")

    first = claim_next_run("worker-one")
    second = claim_next_run("worker-two")

    assert first is not None and second is not None
    assert first.team_id != second.team_id


def test_a_parked_run_does_not_hold_a_concurrency_slot(seeded, monkeypatch):
    """A run waiting on a person holds no worker, so it must not count against
    the number of workers a team may use."""
    monkeypatch.setattr(settings, "comrade_agent_max_running_per_team", 1)
    from shared.agent_runs import pause_for_permission

    parked = _thread(TEAM_A)
    other = _new_thread(TEAM_A, "Meanwhile")
    enqueue_turn(TEAM_A, A1, parked, "needs approval")
    first = claim_next_run("worker-one")
    assert first is not None
    pause_for_permission(TEAM_A, first.id, None)
    enqueue_turn(TEAM_A, A2, other, "unrelated work")

    assert claim_next_run("worker-two") is not None


def test_a_worker_that_lost_its_lease_stops_at_its_next_step(seeded):
    """🔴 It did not. `renew_lease` returning False ended the renewer thread
    and told nothing else, so a worker whose run had been handed to somebody
    else kept calling the model and writing steps into it."""
    from agent.effects import run_is_active

    thread = _thread(TEAM_A)
    run_id = str(enqueue_turn(TEAM_A, A1, thread, "long job"))
    assert claim_next_run("worker-one") is not None
    assert run_is_active(TEAM_A, run_id, worker_id="worker-one")

    _expire_lease(run_id)
    recover_expired_runs()
    assert claim_next_run("worker-two") is not None

    assert not run_is_active(TEAM_A, run_id, worker_id="worker-one")
    assert run_is_active(TEAM_A, run_id, worker_id="worker-two")


def test_every_slot_in_one_process_has_its_own_identity(seeded):
    """The lease fence is BY worker id, so two slots sharing one would each
    read the other's run as their own."""
    import threading
    from unittest.mock import patch

    from agent import worker

    seen: list[str] = []
    lock = threading.Lock()

    def _record(worker_id):
        with lock:
            seen.append(worker_id)
            if len(set(seen)) >= 2:
                worker._stopping.set()
        return False

    with patch.object(worker, "run_once", _record), \
         patch.object(worker.settings, "comrade_agent_concurrency", 2):
        worker._stopping.clear()
        try:
            worker.main()
        finally:
            worker._stopping.set()

    assert len(set(seen)) >= 2, f"slots shared an identity: {seen}"
