"""A busy worker is not a dead one.

🔴 THE DEFECT (fix.md F39). The heartbeat was not on its own clock.

F33 gave workers a heartbeat so `/ready` could tell "nobody is here" from "no
work to do" — the right fix for a stack whose queues were empty because both
workers were stopped. But both call sites beat and then BLOCK:

    _beater.maybe(probe_capabilities)
    if not run_once(worker_id):        # a whole agent turn
    ...
    beater.maybe()
    if tick() == 0:                    # a whole pipeline batch

`STALE_SECONDS` is 120. An agent turn that holds every slot for longer than
that — a long tool-using run, a slow model, a big repository sync — stops the
beats, and readiness declares live workers missing. So the check added to stop
a quiet deployment lying about being ready acquired the opposite failure: a
BUSY deployment lying about being dead.

Liveness has to be emitted by something that is not the thing doing the work.

WHAT A BEAT DOES NOT MEAN. It says this process is alive, and nothing about
whether its current job is progressing. That distinction is deliberate: a
wedged job must still be diagnosable, and it is — through the lease expiry that
`/ready` reports separately. A heartbeat that implied progress would hide
exactly the case leases exist to catch.
"""
import threading
import time

import psycopg
import pytest

from shared import heartbeat
from shared.config import settings
from shared.heartbeat import BEAT_SECONDS, Pulse, live_workers


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


@pytest.fixture
def quiet(seeded):
    """An empty heartbeat table, before AND after.

    The `after` half is not tidiness. These tests write REAL heartbeats for the
    real `agent` kind — the check constraint allows only 'agent' and 'pipeline'
    — mostly with no capabilities, and `/ready` reads capabilities off live
    agent heartbeats to answer its `sandbox` check. Cleaning only on the way in
    left a capability-less agent row behind for whatever ran next, and
    tests/test_readiness.py failed on it in a full run while passing alone.
    """
    conn = _admin()
    try:
        conn.execute("delete from public.worker_heartbeats")
        yield None
        conn.execute("delete from public.worker_heartbeats")
    finally:
        conn.close()


def _beats(kind: str) -> int:
    conn = _admin()
    try:
        row = conn.execute(
            "select count(*) from public.worker_heartbeats where worker_kind=%s",
            (kind,),
        ).fetchone()
    finally:
        conn.close()
    return row[0]


# ---------------------------------------------------------------------------

def test_a_pulse_beats_while_the_worker_is_blocked(quiet, monkeypatch):
    """🔴 The finding's own case, at speed. The work never returns; the beats
    have to happen anyway."""
    monkeypatch.setattr(heartbeat, "BEAT_SECONDS", 0.05)
    beats: list[float] = []
    real_beat = heartbeat.beat

    def _counted(kind, **kwargs):
        beats.append(time.monotonic())
        real_beat(kind, **kwargs)

    monkeypatch.setattr(heartbeat, "beat", _counted)

    with Pulse("agent"):
        # Exactly what an agent turn or a pipeline batch does to this thread.
        time.sleep(0.4)

    assert len(beats) >= 3, (
        f"only {len(beats)} beats while the worker was busy; readiness would"
        " have called it dead"
    )


def test_the_pulse_stops_with_the_worker(quiet, monkeypatch):
    """"Shut down the heartbeat with the worker." A thread that outlives its
    process's work keeps a stopped worker looking alive, which is the original
    F33 defect wearing a different hat."""
    monkeypatch.setattr(heartbeat, "BEAT_SECONDS", 0.05)
    counted: list[int] = []
    real_beat = heartbeat.beat

    monkeypatch.setattr(heartbeat, "beat",
                        lambda kind, **kw: (counted.append(1),
                                            real_beat(kind, **kw))[1])

    pulse = Pulse("agent")
    pulse.start()
    time.sleep(0.2)
    pulse.stop()
    after_stop = len(counted)
    time.sleep(0.3)

    assert len(counted) == after_stop, "the pulse kept beating after stop()"


def test_stopping_does_not_wait_for_the_next_beat(quiet, monkeypatch):
    """A drain that takes BEAT_SECONDS to let go of a thread makes every
    deployment slower for a heartbeat nobody is reading by then."""
    monkeypatch.setattr(heartbeat, "BEAT_SECONDS", 30.0)
    pulse = Pulse("agent")
    pulse.start()

    started = time.monotonic()
    pulse.stop()
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"stop() took {elapsed:.1f}s"


def test_the_first_beat_does_not_wait_for_the_interval(quiet):
    """A worker that has just come up should be visible immediately, not in
    thirty seconds — a rolling restart is exactly when readiness is read."""
    with Pulse("agent"):
        deadline = time.monotonic() + 5
        while _beats("agent") == 0 and time.monotonic() < deadline:
            time.sleep(0.05)

    assert _beats("agent") == 1


def test_capabilities_are_probed_only_when_a_beat_happens(quiet, monkeypatch):
    """The sandbox probe shells out. Running it on a timer is fine; running it
    per loop pass was what made the callable form necessary, and the pulse must
    not lose that."""
    monkeypatch.setattr(heartbeat, "BEAT_SECONDS", 0.05)
    probes: list[int] = []

    def _probe():
        probes.append(1)
        return {"sandbox": "ok"}

    with Pulse("agent", capabilities=_probe):
        time.sleep(0.25)

    assert 1 <= len(probes) <= 8, f"{len(probes)} probes"
    conn = _admin()
    try:
        stored = conn.execute(
            "select capabilities from public.worker_heartbeats"
            " where worker_kind='agent'",
        ).fetchone()[0]
    finally:
        conn.close()
    assert stored == {"sandbox": "ok"}


def test_a_database_failure_does_not_kill_the_pulse(quiet, monkeypatch):
    """The beat already refuses to raise. The thread around it must not turn a
    transient database problem into a permanently silent worker — readiness
    noticing the silence is the intended signal, and it has to be able to stop
    noticing again."""
    monkeypatch.setattr(heartbeat, "BEAT_SECONDS", 0.05)
    attempts: list[int] = []

    def _flaky(kind, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("connection refused")

    monkeypatch.setattr(heartbeat, "beat", _flaky)

    with Pulse("agent"):
        time.sleep(0.3)

    assert len(attempts) >= 4, "the pulse gave up after a failed beat"


def test_a_busy_agent_slot_leaves_the_process_visible(quiet, monkeypatch):
    """🔴 The reproduction, run against the real worker loop.

    Measured before the fix, with the intervals scaled down: one turn holding a
    slot for 1.2s against a 0.4s staleness window left the heartbeat 1.1s old
    and readiness answering NO WORKER. The claim slot must be able to block
    without the process disappearing from `/ready`.
    """
    from agent import worker as agent_worker

    monkeypatch.setattr(heartbeat, "BEAT_SECONDS", 0.05)
    monkeypatch.setattr(agent_worker, "_stopping", threading.Event())

    def _one_long_turn(worker_id):
        time.sleep(0.5)
        agent_worker._stopping.set()
        return True

    monkeypatch.setattr(agent_worker, "run_once", _one_long_turn)

    with Pulse("agent"):
        agent_worker._loop("probe-slot")
        # Freshness is the whole question: how old is the beat by the time the
        # blocking turn returns?
        reported = live_workers(within=1)

    assert "agent" in reported, (
        "the worker was working and readiness could not see it"
    )
    assert reported["agent"][0]["age_seconds"] <= 1


def test_the_work_loop_itself_no_longer_beats(quiet, monkeypatch):
    """The other half: liveness must not be coming from the loop, or it is
    still hostage to whatever the loop is doing."""
    from agent import worker as agent_worker

    monkeypatch.setattr(heartbeat, "BEAT_SECONDS", 0.01)
    monkeypatch.setattr(agent_worker, "_stopping", threading.Event())

    def _one_turn(worker_id):
        agent_worker._stopping.set()
        return True

    monkeypatch.setattr(agent_worker, "run_once", _one_turn)

    agent_worker._loop("probe-slot")          # no Pulse around it

    assert _beats("agent") == 0, (
        "the work loop beat on its own, so a busy loop still silences liveness"
    )


def test_a_live_worker_is_reported_while_it_is_busy(quiet, monkeypatch):
    """End to end, through the function readiness actually calls."""
    monkeypatch.setattr(heartbeat, "BEAT_SECONDS", 0.05)

    with Pulse("agent"):
        time.sleep(0.2)
        reported = live_workers()

    assert "agent" in reported
    assert reported["agent"][0]["age_seconds"] <= 2
