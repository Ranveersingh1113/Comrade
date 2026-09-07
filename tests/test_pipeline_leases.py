"""Pipeline jobs: ownership, backoff, long leases, and bounded drains.

🔴 THE DEFECTS.

`_finish` updated `where id = %s` and nothing else. A worker whose lease
expired — and whose job another worker had already reclaimed — still stamped
its own result over the new claim. Two workers, one job, and the second one's
work discarded by the first one's late answer.

A failed job went straight back to `pending` and was re-claimed on the very
next iteration, so its three attempts burned in a few milliseconds against
whatever was already broken. Retrying is only useful if something has had time
to change.

The lease was a flat thirty minutes with no renewal, so any job that honestly
took longer was declared abandoned while it was still running.

And `tick()` drained until the queue was empty before doing any maintenance,
so under continuous ingestion the chat sweep, the disk cap and the sandbox
reconciler never ran at all.
"""
import time

import psycopg
import pytest

from pipeline import worker
from shared.config import settings
from tests._seed import TEAM_A


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _clear_jobs() -> None:
    conn = _admin()
    try:
        conn.execute("delete from public.jobs")
    finally:
        conn.close()


def _enqueue(job_type: str = "parse_document", payload: dict | None = None):
    from psycopg.types.json import Json

    conn = _admin()
    try:
        return conn.execute(
            "insert into public.jobs (team_id, job_type, payload) values (%s,%s,%s)"
            " returning id",
            (TEAM_A, job_type, Json(payload or {})),
        ).fetchone()[0]
    finally:
        conn.close()


def _row(job_id):
    conn = _admin()
    try:
        return conn.execute(
            "select status, attempts, worker_id, available_at, lease_expires_at"
            " from public.jobs where id=%s",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()


def _expire_lease(job_id) -> None:
    conn = _admin()
    try:
        conn.execute(
            "update public.jobs set lease_expires_at = now() - interval '1 hour'"
            " where id=%s",
            (job_id,),
        )
    finally:
        conn.close()


def _make_available(job_id) -> None:
    conn = _admin()
    try:
        conn.execute(
            "update public.jobs set available_at = now() - interval '1 second'"
            " where id=%s",
            (job_id,),
        )
    finally:
        conn.close()


def _boom(_team_id, _payload):
    raise RuntimeError("kaboom")


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------

def test_a_worker_that_lost_its_job_cannot_finish_it(seeded):
    """The late answer must not land on somebody else's claim."""
    _clear_jobs()
    job_id = _enqueue()
    assert worker.run_once(handlers={"parse_document": lambda *_: None},
                           worker_id="worker-one") is True
    # Pretend it stalled instead: put the job back out for reclaiming.
    conn = _admin()
    try:
        conn.execute(
            "update public.jobs set status='processing', finished_at=null,"
            " lease_expires_at=now() - interval '1 hour', worker_id='worker-two'"
            " where id=%s",
            (job_id,),
        )
    finally:
        conn.close()

    worker._finish(job_id, "done", worker_id="worker-one")

    status, _, owner, _, _ = _row(job_id)
    assert owner == "worker-two"
    assert status == "processing", "worker-one must not close worker-two's job"


def test_the_worker_holding_the_claim_still_finishes_normally(seeded):
    _clear_jobs()
    job_id = _enqueue()
    done = []

    assert worker.run_once(
        handlers={"parse_document": lambda *_: done.append(1)},
        worker_id="worker-one",
    ) is True

    status, _, owner, _, _ = _row(job_id)
    assert done == [1]
    assert status == "done" and owner == "worker-one"


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------

def test_a_failed_job_waits_before_it_is_retried(seeded):
    """🔴 It did not. The retry was claimable on the very next iteration, so
    three attempts burned in milliseconds against a thing that had had no time
    to change."""
    _clear_jobs()
    job_id = _enqueue()

    assert worker.run_once(handlers={"parse_document": _boom}) is True

    status, attempts, _, available_at, _ = _row(job_id)
    assert status == "pending" and attempts == 1
    conn = _admin()
    try:
        future = conn.execute(
            "select available_at > now() from public.jobs where id=%s", (job_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert future, "a retry that is instantly claimable is not a retry"
    # And it is genuinely not claimable yet.
    assert worker.run_once(handlers={"parse_document": _boom}) is False


def test_the_wait_grows_with_each_attempt(seeded):
    """Otherwise a permanently broken dependency is hammered at a fixed rate."""
    _clear_jobs()
    job_id = _enqueue()

    waits = []
    for _ in range(2):
        worker.run_once(handlers={"parse_document": _boom})
        conn = _admin()
        try:
            waits.append(conn.execute(
                "select extract(epoch from (available_at - now()))"
                " from public.jobs where id=%s",
                (job_id,),
            ).fetchone()[0])
        finally:
            conn.close()
        _make_available(job_id)

    assert waits[1] > waits[0]


def test_a_permanent_failure_is_not_given_a_retry_window(seeded):
    """It will never succeed, so scheduling it again is only noise."""
    _clear_jobs()
    job_id = _enqueue()

    def _never(_team_id, _payload):
        raise worker.PermanentJobError("this payload cannot work")

    worker.run_once(handlers={"parse_document": _never})

    status, _, _, _, _ = _row(job_id)
    assert status == "failed"


# ---------------------------------------------------------------------------
# Long leases
# ---------------------------------------------------------------------------

def test_a_long_job_keeps_its_lease_while_it_works(seeded, monkeypatch):
    """🔴 The lease was a flat thirty minutes with no renewal, so a job that
    honestly took longer was declared abandoned while it was still running."""
    _clear_jobs()
    job_id = _enqueue()
    monkeypatch.setattr(worker, "RENEW_SECONDS", 0.2)
    seen: list = []

    def _slow(_team_id, _payload):
        # Shorten the lease from under the handler; the renewer must push it
        # back out while the work is still going.
        conn = _admin()
        try:
            conn.execute(
                "update public.jobs set lease_expires_at = now() + interval '2 seconds'"
                " where id=%s",
                (job_id,),
            )
        finally:
            conn.close()
        time.sleep(1.0)
        seen.append(_row(job_id)[4])

    worker.run_once(handlers={"parse_document": _slow}, worker_id="worker-one")

    conn = _admin()
    try:
        far_out = conn.execute(
            "select %s > now() + interval '1 minute'", (seen[0],)
        ).fetchone()[0]
    finally:
        conn.close()
    assert far_out, "the lease was never renewed while the handler ran"


def test_renewal_belongs_to_the_worker_that_holds_the_claim(seeded):
    _clear_jobs()
    job_id = _enqueue()
    worker.run_once(handlers={"parse_document": lambda *_: None},
                    worker_id="worker-one")
    conn = _admin()
    try:
        conn.execute(
            "update public.jobs set status='processing', worker_id='worker-one',"
            " lease_expires_at=now() + interval '1 minute' where id=%s",
            (job_id,),
        )
    finally:
        conn.close()

    assert worker.renew_job_lease(job_id, "worker-one") is True
    assert worker.renew_job_lease(job_id, "someone-else") is False


# ---------------------------------------------------------------------------
# Bounded drain
# ---------------------------------------------------------------------------

def test_maintenance_still_runs_under_continuous_ingestion(seeded, monkeypatch):
    """🔴 `tick()` drained until the queue was EMPTY before sweeping anything.
    A team ingesting steadily meant the chat sweep, the disk cap and the
    sandbox reconciler never ran once."""
    _clear_jobs()
    monkeypatch.setattr(worker, "MAX_DRAIN_BATCH", 3)
    for i in range(10):
        _enqueue(payload={"document_id": str(i)})

    swept = []
    monkeypatch.setattr("pipeline.chat.sweep_chat_compiles", lambda: swept.append(1) or [])
    original = worker._HANDLERS.get("parse_document")
    worker.register("parse_document", lambda *_: None)
    try:
        processed = worker.tick()
    finally:
        if original is not None:
            worker._HANDLERS["parse_document"] = original
        else:
            worker._HANDLERS.pop("parse_document", None)

    assert processed == 3, "the drain must be bounded, not run to exhaustion"
    assert swept == [1], "maintenance runs even with work still queued"


def test_a_draining_worker_stops_claiming_new_jobs(seeded, monkeypatch):
    """Shutdown means finish what is in hand, not start more."""
    _clear_jobs()
    for i in range(3):
        _enqueue(payload={"document_id": str(i)})
    original = worker._HANDLERS.get("parse_document")
    worker.register("parse_document", lambda *_: worker._stopping.set())
    worker._stopping.clear()
    try:
        processed = worker.tick()
    finally:
        worker._stopping.clear()
        if original is not None:
            worker._HANDLERS["parse_document"] = original
        else:
            worker._HANDLERS.pop("parse_document", None)

    assert processed == 1, "it took the one in hand and claimed nothing more"


def test_sweeps_run_on_their_own_clock_not_once_per_tick(seeded, monkeypatch):
    """🔴 They ran every tick, which was survivable only because a tick drained
    the WHOLE queue first and therefore happened rarely. Bounding the drain
    would otherwise have turned a five-second poll into a five-second full disk
    scan and Docker reconciliation — replacing starvation with a stampede."""
    _clear_jobs()
    # A long interval on purpose: a tick does real workspace and Docker work,
    # so with the production 30s the assertion would turn on how slow this
    # machine happened to be rather than on the gating.
    monkeypatch.setattr(worker, "CHAT_SWEEP_SECONDS", 3600.0)
    swept = []
    monkeypatch.setattr("pipeline.chat.sweep_chat_compiles", lambda: swept.append(1) or [])

    worker.tick()
    worker.tick()
    worker.tick()

    assert swept == [1], "the sweep ran once, not once per poll"
