"""Stopping a worker on purpose, and what a member loses when you do.

🔴 THE DEFECT. The pipeline worker's batch guard read
`if _stopping.is_set() and processed: break`, so a stop that arrived while the
worker was idle claimed ONE MORE job before noticing — `processed` is 0 at the
top of the batch, and the guard needs it to be non-zero. A job claimed at the
instant of SIGTERM has the whole shutdown grace period to finish or it is
killed mid-flight and waits out its entire lease before anyone redoes it.
"Finish what is in hand, do not start more" is the intent the comment above it
states; the condition said something else.
"""
import threading

import psycopg

from pipeline import worker
from shared.config import settings
from shared.db import Role, team_session
from tests._seed import TEAM_A


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _clear():
    conn = _admin()
    try:
        conn.execute("delete from public.jobs")
    finally:
        conn.close()


def _queue(n: int) -> None:
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        for i in range(n):
            conn.execute(
                "insert into public.jobs (team_id, job_type, payload, dedupe_key)"
                " values (%s,'ingest_github','{}',%s)",
                (TEAM_A, f"drain-{i}"),
            )


def _pending() -> int:
    conn = _admin()
    try:
        return conn.execute(
            "select count(*) from public.jobs where status='pending'"
        ).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------

def test_a_stop_takes_no_new_work(seeded, monkeypatch):
    """🔴 It took one more. The guard required `processed` to be non-zero, and
    at the top of a batch it is zero — so a stop arriving while the worker was
    idle still claimed a job, which then had the whole shutdown grace period to
    finish or be killed mid-flight."""
    claimed: list[int] = []
    monkeypatch.setattr(
        worker, "run_once", lambda *a, **k: claimed.append(1) or True,
    )
    worker._stopping.set()
    try:
        worker.tick()
    finally:
        worker._stopping.clear()

    assert claimed == [], "a stopping worker claimed work it did not have to"


def test_a_stop_mid_batch_finishes_the_item_in_hand(seeded, monkeypatch):
    """The other half, and the reason this is a drain rather than a kill: the
    job already being worked on is completed, not abandoned to its lease."""
    handled: list[int] = []

    def _one_job(*_a, **_k):
        handled.append(1)
        if len(handled) == 2:
            # The signal lands while this one is in hand. It must still
            # finish, and nothing after it may be claimed.
            worker._stopping.set()
        return True

    monkeypatch.setattr(worker, "run_once", _one_job)
    try:
        worker.tick()
    finally:
        worker._stopping.clear()

    assert len(handled) == 2, (
        f"claimed {len(handled)} items; the one in hand should finish and no"
        " more should start"
    )


def test_a_worker_that_is_not_stopping_drains_its_whole_batch(seeded, monkeypatch):
    """The guard must not fire on an ordinary tick — a worker that stops after
    one job per poll is a worker that never keeps up."""
    handled: list[int] = []
    monkeypatch.setattr(
        worker, "run_once", lambda *a, **k: handled.append(1) or True,
    )

    worker.tick()

    assert len(handled) == worker.MAX_DRAIN_BATCH


def test_work_a_killed_worker_was_holding_comes_back(seeded):
    """The measured window. A worker killed outright — the grace period ran
    out, the host went away — leaves its claim behind, and the only thing that
    returns that job to the queue is its lease expiring."""
    _clear()
    _queue(1)

    # Claimed, then the process disappears: no _finish, no lease renewal.
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        pass
    conn = _admin()
    try:
        conn.execute(
            "update public.jobs set status='processing', worker_id='killed',"
            " picked_at=now(), lease_expires_at=now() - interval '1 second'"
        )
    finally:
        conn.close()

    assert worker.run_once(
        handlers={"ingest_github": lambda t, p: None}, worker_id="recovery",
    ), "an expired lease left the job unclaimable"


def test_the_recovery_window_is_stated_rather_than_implied(seeded):
    """An operator sizing a shutdown grace period needs this number, and it
    should not have to be reverse-engineered from an f-string."""
    assert worker.LEASE_INTERVAL, "the lease window has to be nameable"
    assert worker.MAX_ATTEMPTS >= 2, (
        "work interrupted by a stop has to get another attempt"
    )


def test_the_agent_worker_stops_claiming_too(seeded, monkeypatch):
    """The same property on the other queue, where the cost is a member
    watching a turn that never starts."""
    from agent import worker as agent_worker

    claims: list[str] = []
    monkeypatch.setattr(
        agent_worker, "run_once",
        lambda worker_id: claims.append(worker_id) or True,
    )
    agent_worker._stopping.set()
    try:
        thread = threading.Thread(target=agent_worker._loop, args=("slot-0",))
        thread.start()
        thread.join(timeout=5)
    finally:
        agent_worker._stopping.clear()

    assert not thread.is_alive(), "the slot did not stop"
    assert claims == [], "a stopping slot claimed another turn"


def test_a_second_signal_is_left_to_the_default_handler():
    """🔴 Both workers' docstrings promised that a second SIGTERM would kill
    the process, and `signal.signal` installs a PERSISTENT handler — it is not
    reset after delivery. So every subsequent signal was swallowed exactly like
    the first, and a worker wedged inside a long job could not be stopped with
    anything short of SIGKILL. An escape hatch that is documented and absent is
    worse than one that was never claimed: it is what an operator reaches for
    when the first attempt did not work.
    """
    import signal

    from agent import worker as agent_worker

    for module in (worker, agent_worker):
        previous = signal.getsignal(signal.SIGTERM)
        try:
            module._drain_on_signal()
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler), f"{module.__name__} installed no handler"

            handler(signal.SIGTERM, None)          # the operator asks once

            assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL, (
                f"{module.__name__} would swallow a second SIGTERM too"
            )
        finally:
            signal.signal(signal.SIGTERM, previous)
            module._stopping.clear()
