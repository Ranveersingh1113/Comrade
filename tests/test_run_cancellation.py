"""Stopping a turn that is already running.

🔴 THE DEFECT. There was no way to. `cancel_run` existed in the queue module
and nothing reached it — no endpoint, no button — so a member who asked the
wrong question, or watched a turn head somewhere expensive, could only wait it
out. The one thing that noticed cancellation at all was `claim_effect`, and
only for a run something else had already marked cancelled.
"""
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from agent.run_queue import claim_next_run, enqueue_turn
from server.app import app
from server.auth import current_user_id
from shared.config import settings
from tests._seed import A1, A2, B1, TEAM_A


def _thread_id() -> str:
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        row = conn.execute(
            "select id from public.threads where team_id=%s and title='General'",
            (TEAM_A,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _row(run_id: str) -> tuple:
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        return conn.execute(
            "select status, last_error, lease_expires_at from public.agent_runs"
            " where id=%s",
            (run_id,),
        ).fetchone()


def _client(user_id: str) -> TestClient:
    app.dependency_overrides[current_user_id] = lambda: user_id
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def test_a_member_can_stop_the_turn_they_asked_for(seeded):
    run_id = str(enqueue_turn(TEAM_A, A1, _thread_id(), "read the whole repository"))

    resp = _client(A1).post(f"/agent/runs/{run_id}/cancel", json={"team_id": TEAM_A})

    assert resp.status_code == 200, resp.text
    status, reason, lease = _row(run_id)
    assert status == "cancelled"
    # The member reads the run row, not the generator. A cancelled turn with no
    # reason reaches them as a blank stop.
    assert reason
    assert lease is None


def test_a_teammate_cannot_stop_a_run_they_did_not_ask_for(seeded):
    """Thread access is not enough. A2 can see this thread and this run; the
    request belongs to A1, and cancelling it is speaking for them."""
    run_id = str(enqueue_turn(TEAM_A, A1, _thread_id(), "keep going"))

    resp = _client(A2).post(f"/agent/runs/{run_id}/cancel", json={"team_id": TEAM_A})

    assert resp.status_code == 403, resp.text
    assert _row(run_id)[0] == "queued"


def test_someone_from_another_team_cannot_stop_it(seeded):
    run_id = str(enqueue_turn(TEAM_A, A1, _thread_id(), "keep going"))

    resp = _client(B1).post(f"/agent/runs/{run_id}/cancel", json={"team_id": TEAM_A})

    assert resp.status_code == 403, resp.text
    assert _row(run_id)[0] == "queued"


def test_cancelling_a_finished_run_is_not_an_error_but_changes_nothing(seeded):
    """Two taps on Stop, or a stop that races the turn's own ending."""
    run_id = str(enqueue_turn(TEAM_A, A1, _thread_id(), "quick one"))
    client = _client(A1)
    client.post(f"/agent/runs/{run_id}/cancel", json={"team_id": TEAM_A})

    resp = client.post(f"/agent/runs/{run_id}/cancel", json={"team_id": TEAM_A})

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"


def test_cancelling_a_queued_run_hands_its_budget_back(seeded, monkeypatch):
    """It never reached the model, so it must not count against the hour.

    The estimate is taken when the turn is admitted, before anything runs — so
    a turn stopped in the queue has spent nothing at all."""
    from shared.usage import reserve_turn, record_reservation

    monkeypatch.setattr(settings, "agent_turns_per_hour", 50)
    run_id = str(enqueue_turn(TEAM_A, A1, _thread_id(), "never mind"))
    record_reservation(TEAM_A, run_id, reserve_turn(TEAM_A))

    _client(A1).post(f"/agent/runs/{run_id}/cancel", json={"team_id": TEAM_A})

    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        finalized = conn.execute(
            "select usage_finalized_at from public.agent_runs where id=%s", (run_id,)
        ).fetchone()[0]
    assert finalized is not None, "a cancelled turn must settle what it reserved"


def test_a_cancelled_run_stops_before_its_next_step(seeded):
    """The effect log already refused a cancelled run's next WRITE. Nothing
    stopped the loop itself: read-only tools kept running and the model kept
    being called, on a turn nobody was waiting for any more."""
    from agent.effects import run_is_active
    from agent.run_queue import cancel_run

    run_id = str(enqueue_turn(TEAM_A, A1, _thread_id(), "long job"))
    run = claim_next_run("worker-one")
    assert run is not None and run.id == run_id
    assert run_is_active(TEAM_A, run.id)

    assert cancel_run(TEAM_A, run.id, requester_id=A1)

    assert not run_is_active(TEAM_A, run.id)


def test_settling_usage_survives_the_run_having_already_left_running(seeded):
    """🔴 `_finish` called `finish_run` first, and `finish_run` refuses a run
    that is no longer `running` — so on the cancellation path it raised and
    `finalize_usage` never ran. The estimate stayed on the team's bucket for
    the rest of the hour, charging them for a turn they stopped."""
    from agent.runtime import _finish
    from agent.run_queue import cancel_run
    from shared.usage import record_reservation, reserve_turn

    run_id = str(enqueue_turn(TEAM_A, A1, _thread_id(), "stop me"))
    run = claim_next_run("worker-one")
    assert run is not None
    record_reservation(TEAM_A, run.id, reserve_turn(TEAM_A))
    cancel_run(TEAM_A, run.id, requester_id=A1)

    _finish(TEAM_A, run.id, "failed", 10, 5, "worker-one")

    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        status, finalized = conn.execute(
            "select status, usage_finalized_at from public.agent_runs where id=%s",
            (run.id,),
        ).fetchone()
    assert status == "cancelled", "a worker's failure must not overwrite the answer"
    assert finalized is not None


# ---------------------------------------------------------------------------
# Propagating the stop into work that is already running
# ---------------------------------------------------------------------------

def test_a_stop_kills_the_container_instead_of_waiting_out_its_timeout():
    """🔴 Nothing reached the container. Stopping a turn stopped the LOOP; a
    test suite already running kept its host CPU for the rest of its timeout —
    up to ten minutes of work for a turn nobody was waiting on any more, which
    is the runaway-container problem the bounded runner exists to prevent,
    arriving through the one door it had left open."""
    import sys
    import time

    from agent.sandbox import run_bounded

    started = time.monotonic()
    result = run_bounded(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        timeout=60, container="comrade-run-nonexistent", limit=1024,
        stop=lambda: True,
    )
    elapsed = time.monotonic() - started

    assert result["cancelled"] is True
    assert not result["timed_out"], "a stop is not a timeout; they mean different things"
    assert elapsed < 20, f"waited {elapsed:.1f}s for a command that was cancelled"


def test_a_command_nobody_stopped_still_runs_to_completion():
    """The poll must not become a way for ordinary work to be killed early."""
    import sys

    from agent.sandbox import run_bounded

    result = run_bounded(
        [sys.executable, "-c", "print('finished')"],
        timeout=60, container="comrade-run-nonexistent", limit=1024,
        stop=lambda: False,
    )

    assert result["exit_code"] == 0
    assert result["cancelled"] is False
    assert "finished" in result["stdout"]


def test_repo_run_hands_the_sandbox_a_way_to_notice_the_stop(monkeypatch):
    """Wiring, not behaviour: the callable has to actually reach the runner."""
    from agent import sandbox

    seen = {}

    def _fake(argv, *, timeout, container, limit, stop=None):
        seen["stop"] = stop
        return {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False,
                "output_limited": False, "cancelled": False}

    monkeypatch.setattr(sandbox, "run_bounded", _fake)
    monkeypatch.setattr(sandbox, "_docker_run_argv", lambda *a, **k: ["docker", "run"])
    monkeypatch.setattr(sandbox.settings, "comrade_sandbox_backend", "docker")

    sandbox.run_contained(
        ["python", "-V"], root=__import__("pathlib").Path("."),
        stop=lambda: True,
    )

    assert seen["stop"] is not None and seen["stop"]() is True

# ---------------------------------------------------------------------------
# F49 — a parked run's reservation is released when it is cancelled
# ---------------------------------------------------------------------------
#
# THE INVARIANT: a run that reaches a terminal state releases its reservation
# exactly once — including when there is no worker left to do it.
#
# A permission wait checkpoints what the run has spent, drops the lease and
# RETURNS from the runtime. Nobody is executing it any more. Cancelling from
# there set a terminal status and settled nothing, because the cancellation
# route only settled runs it found `queued`. The estimate stayed on the hour's
# bucket, so a 6,000-token reservation kept consuming the team's admission
# budget after a turn that really spent 1,200 was stopped.


def _bucket_tokens() -> int:
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        row = conn.execute(
            "select tokens from public.usage_buckets where team_id=%s"
            " and bucket=date_trunc('hour', now())", (TEAM_A,),
        ).fetchone()
    return row[0] if row else 0


def _reserve(run_id: str, tokens: int) -> None:
    """The admission reservation, as claim_budget leaves it."""
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        conn.execute("delete from public.usage_buckets where team_id=%s", (TEAM_A,))
        conn.execute(
            "insert into public.usage_buckets (team_id, bucket, turns, tokens)"
            " values (%s, date_trunc('hour', now()), 1, %s)", (TEAM_A, tokens))
        conn.execute(
            "update public.agent_runs set tokens_reserved=%s,"
            " usage_bucket=date_trunc('hour', now()) where id=%s",
            (tokens, run_id))


def _usage(run_id: str) -> tuple:
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        return conn.execute(
            "select coalesce(input_tokens,0), coalesce(output_tokens,0),"
            " usage_finalized_at is not null from public.agent_runs where id=%s",
            (run_id,),
        ).fetchone()


def test_cancelling_a_parked_run_releases_its_reservation(seeded):
    """🔴 THE DEFECT (fix.md F49). The worker has already returned, so no
    settlement is ever coming from it, and the route settled only `queued`
    runs. The reservation stayed charged for the rest of the hour."""
    from shared.agent_runs import pause_for_permission

    run_id = str(enqueue_turn(TEAM_A, A1, _thread_id(), "ask before deleting"))
    claimed = claim_next_run("worker-1")
    assert claimed is not None and str(claimed.id) == run_id
    _reserve(run_id, 6000)
    # The real park: checkpoints usage, drops the lease, worker returns.
    pause_for_permission(TEAM_A, run_id, "worker-1", 900, 300)

    resp = _client(A1).post(f"/agent/runs/{run_id}/cancel", json={"team_id": TEAM_A})

    assert resp.status_code == 200, resp.text
    assert _row(run_id)[0] == "cancelled"
    _, _, finalized = _usage(run_id)
    assert finalized, "the run was never settled, so its estimate is still charged"
    # 6000 reserved, 1200 actually spent: the hour keeps what the turn cost.
    assert _bucket_tokens() == 1200


def test_a_parked_run_is_settled_only_once(seeded):
    """Cancelling twice must not refund the reservation twice."""
    from shared.agent_runs import pause_for_permission

    run_id = str(enqueue_turn(TEAM_A, A1, _thread_id(), "ask first"))
    claim_next_run("worker-1")
    _reserve(run_id, 6000)
    pause_for_permission(TEAM_A, run_id, "worker-1", 900, 300)
    client = _client(A1)

    first = client.post(f"/agent/runs/{run_id}/cancel", json={"team_id": TEAM_A})
    second = client.post(f"/agent/runs/{run_id}/cancel", json={"team_id": TEAM_A})

    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["already_finished"] is True
    assert _bucket_tokens() == 1200


def test_a_resumed_run_cancelled_before_its_next_claim_keeps_what_it_spent(seeded):
    """Approval requeues the run. It is `queued` again, but it is NOT a run
    that never executed — an earlier segment really spent tokens, and the
    checkpoint on the row is what says so.

    Settling zero here, which is what the queued branch did, would release
    budget the team actually used."""
    from shared.agent_runs import pause_for_permission

    run_id = str(enqueue_turn(TEAM_A, A1, _thread_id(), "ask, then continue"))
    claim_next_run("worker-1")
    _reserve(run_id, 6000)
    pause_for_permission(TEAM_A, run_id, "worker-1", 900, 300)
    # Approval puts it back in the queue, with the checkpoint intact.
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        conn.execute("update public.agent_runs set status='queued' where id=%s",
                     (run_id,))

    resp = _client(A1).post(f"/agent/runs/{run_id}/cancel", json={"team_id": TEAM_A})

    assert resp.status_code == 200, resp.text
    assert _bucket_tokens() == 1200, (
        "a resumed run was settled as if it had never run"
    )


def test_a_run_that_never_executed_still_settles_nothing(seeded):
    """The case that already worked, and must keep working: queued, never
    claimed, no checkpoint. Its whole estimate goes back."""
    run_id = str(enqueue_turn(TEAM_A, A1, _thread_id(), "never started"))
    _reserve(run_id, 6000)

    resp = _client(A1).post(f"/agent/runs/{run_id}/cancel", json={"team_id": TEAM_A})

    assert resp.status_code == 200, resp.text
    assert _bucket_tokens() == 0


def test_cancelling_an_executing_run_leaves_settlement_to_its_worker(seeded):
    """The fence F43 put here stays. A worker is holding this run and is the
    only thing that knows what the turn has spent, so the server must not
    settle a stale number from underneath it."""
    run_id = str(enqueue_turn(TEAM_A, A1, _thread_id(), "still going"))
    claim_next_run("worker-1")
    _reserve(run_id, 6000)

    resp = _client(A1).post(f"/agent/runs/{run_id}/cancel", json={"team_id": TEAM_A})

    assert resp.status_code == 200, resp.text
    _, _, finalized = _usage(run_id)
    assert not finalized, "the executing worker settles this one on its way out"
    assert _bucket_tokens() == 6000

# ---------------------------------------------------------------------------
# F49 follow-up — a checkpoint is only final if nobody has run since
# ---------------------------------------------------------------------------


def _expire_lease(run_id: str) -> None:
    """Five minutes of nothing from the worker, without waiting five minutes."""
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        conn.execute(
            "update public.agent_runs set lease_expires_at = now() - interval"
            " '1 minute' where id=%s", (run_id,))


def _owner(run_id: str) -> tuple:
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        return conn.execute(
            "select status, worker_id, usage_owner from public.agent_runs"
            " where id=%s", (run_id,)).fetchone()


def test_a_lease_recovered_run_is_not_settled_from_its_stale_checkpoint(seeded):
    """🔴 THE DEFECT (fix.md, sixth review). `queued` does not mean the old
    execution finished.

    Lease recovery requeues an expired run and clears `worker_id` while the old
    worker may still have an in-flight model response — one that has been PAID
    for and whose usage nothing has checkpointed. Cancelling in that interval
    found a terminal run with no worker and settled its stale checkpoint,
    possibly zero. When the paid response landed, the previous owner's exit
    settlement lost to the `usage_finalized_at` already written, and the tokens
    were discarded.

    Terminal plus unleased is not the same question as "the record of what this
    run spent is complete".
    """
    run_id = str(enqueue_turn(TEAM_A, A1, _thread_id(), "expensive question"))
    claim_next_run("old-worker")
    _reserve(run_id, 6000)
    # The lease lapses while a model response is still in flight. Nothing
    # checkpoints it — that is the whole point.
    _expire_lease(run_id)
    from agent.run_queue import recover_expired_runs
    recover_expired_runs()
    status, worker, owner = _owner(run_id)
    assert (status, worker, owner) == ("queued", None, "old-worker"), (
        status, worker, owner)

    resp = _client(A1).post(f"/agent/runs/{run_id}/cancel", json={"team_id": TEAM_A})

    assert resp.status_code == 200, resp.text
    _, _, finalized = _usage(run_id)
    assert not finalized, (
        "the server settled a run whose last execution never reported what it"
        " spent, so the paid response can no longer be accounted for"
    )
    assert _bucket_tokens() == 6000

    # And when the paid response finally arrives, the worker that made it can
    # still account for it — which is what the reservation was being held for.
    from shared.usage import finalize_usage
    finalize_usage(TEAM_A, run_id, 1200, worker_id="old-worker")

    assert _bucket_tokens() == 1200


def test_a_failed_settlement_does_not_strand_the_reservation(seeded):
    """🔴 THE DEFECT (fix.md, sixth review). Cancellation committed before
    settlement opened its own transaction.

    If the settlement write failed, the run was durably cancelled and still
    reserved — and retrying STOP made it worse, because `cancel_run` then found
    nothing to cancel, returned None, and the route skipped settlement
    entirely. No worker remains for a parked run, so nothing else was coming.
    """
    from shared.agent_runs import pause_for_permission
    import agent.run_queue as run_queue

    run_id = str(enqueue_turn(TEAM_A, A1, _thread_id(), "ask before deleting"))
    claim_next_run("worker-1")
    _reserve(run_id, 6000)
    pause_for_permission(TEAM_A, run_id, "worker-1", 900, 300)

    calls = {"n": 0}
    real = run_queue.settle_cancelled

    def _fails_once(conn, team_id, run_id_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("settlement write failed")
        return real(conn, team_id, run_id_)

    client = _client(A1)
    run_queue.settle_cancelled = _fails_once
    try:
        with pytest.raises(RuntimeError):
            client.post(f"/agent/runs/{run_id}/cancel", json={"team_id": TEAM_A})

        # Nothing durable happened: the cancellation rolled back with the
        # settlement it belongs to, so the member's retry has something to do.
        assert _row(run_id)[0] == "waiting_for_permission"
        assert _bucket_tokens() == 6000

        resp = client.post(f"/agent/runs/{run_id}/cancel", json={"team_id": TEAM_A})
    finally:
        run_queue.settle_cancelled = real

    assert resp.status_code == 200, resp.text
    assert _row(run_id)[0] == "cancelled"
    assert _bucket_tokens() == 1200
    assert calls["n"] == 2, "the retry has to actually attempt settlement again"


def test_a_cancelled_run_left_unsettled_is_repaired_by_a_retry(seeded):
    """The recovery path for a reservation stranded by anything else — an older
    code path, or a crash between two writes that are no longer two writes.

    Retrying STOP on an already-cancelled run settles it if it is eligible, and
    the eligibility rule is what makes that safe: a lease-recovered or still
    executing run is refused, so this cannot discard unaccounted usage."""
    from shared.agent_runs import pause_for_permission
    from agent.run_queue import cancel_run

    run_id = str(enqueue_turn(TEAM_A, A1, _thread_id(), "ask first"))
    claim_next_run("worker-1")
    _reserve(run_id, 6000)
    pause_for_permission(TEAM_A, run_id, "worker-1", 900, 300)
    # Cancelled by something that did not settle it.
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        conn.execute(
            "update public.agent_runs set status='cancelled', finished_at=now(),"
            " worker_id=null where id=%s", (run_id,))
    assert _bucket_tokens() == 6000

    resp = _client(A1).post(f"/agent/runs/{run_id}/cancel", json={"team_id": TEAM_A})

    assert resp.status_code == 200, resp.text
    assert resp.json()["already_finished"] is True
    assert _bucket_tokens() == 1200
    assert cancel_run(TEAM_A, run_id, requester_id=A1) is None


def test_a_retry_does_not_settle_a_lease_recovered_run(seeded):
    """The two halves together: the repair path above must not become a way to
    discard the usage the first test protects."""
    run_id = str(enqueue_turn(TEAM_A, A1, _thread_id(), "expensive question"))
    claim_next_run("old-worker")
    _reserve(run_id, 6000)
    _expire_lease(run_id)
    from agent.run_queue import recover_expired_runs
    recover_expired_runs()
    client = _client(A1)
    client.post(f"/agent/runs/{run_id}/cancel", json={"team_id": TEAM_A})

    client.post(f"/agent/runs/{run_id}/cancel", json={"team_id": TEAM_A})

    _, _, finalized = _usage(run_id)
    assert not finalized
    assert _bucket_tokens() == 6000

# ---------------------------------------------------------------------------
# F50 — a column added yesterday cannot testify about last week
# ---------------------------------------------------------------------------
#
# 🔴 The upgrade acceptance for F50 used to live here, and it dropped
# `usage_owner` and `usage_checkpoint_at` from the CONFIGURED database to build
# a pre-upgrade schema. Re-applying the migrations put the definitions back and
# lost every value in them, for every run in the database — and an interruption
# between the two left the schema with no settlement columns at all. In the
# ordinary gate, not a reset lane (fix.md F51).
#
# It now runs against a disposable restored copy: tests/test_settlement_upgrade.py.
# What stays here is the part that needs no DDL.


def test_a_claim_that_never_incremented_attempts_is_still_refused(seeded):
    """Belt and braces on the new evidence itself.

    `attempts = 0` is trustworthy because the claim is the only thing that
    starts execution and it always increments. If some future path ever hands a
    run to a worker without doing so, this run would look never-claimed while
    having an owner — and settling it would be the same mistake in a new
    costume. Refused on the owner as well.
    """
    from shared.usage import settle_cancelled
    from shared.db import Role, team_session

    run_id = str(enqueue_turn(TEAM_A, A1, _thread_id(), "impossible shape"))
    _reserve(run_id, 6000)
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        conn.execute(
            "update public.agent_runs set status='cancelled', finished_at=now(),"
            " attempts=0, worker_id=null, usage_owner='ghost' where id=%s",
            (run_id,))

    with team_session(Role.AGENT, TEAM_A) as conn:
        settle_cancelled(conn, TEAM_A, run_id)

    _, _, finalized = _usage(run_id)
    assert not finalized
    assert _bucket_tokens() == 6000
