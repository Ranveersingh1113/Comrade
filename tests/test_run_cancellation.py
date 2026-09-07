"""Stopping a turn that is already running.

🔴 THE DEFECT. There was no way to. `cancel_run` existed in the queue module
and nothing reached it — no endpoint, no button — so a member who asked the
wrong question, or watched a turn head somewhere expensive, could only wait it
out. The one thing that noticed cancellation at all was `claim_effect`, and
only for a run something else had already marked cancelled.
"""
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
