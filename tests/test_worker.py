"""Job worker spine: claim/dispatch/complete, retries, and SKIP LOCKED."""
import psycopg

from pipeline import worker
from shared.config import settings
from tests._seed import TEAM_A


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _clear_jobs():
    # the seed inserts one job; remove it so tests control the queue exactly
    conn = _admin()
    try:
        conn.execute("delete from public.jobs")
    finally:
        conn.close()


def _enqueue(job_type="parse_document", payload=None):
    conn = _admin()
    try:
        from psycopg.types.json import Json
        return conn.execute(
            "insert into public.jobs (team_id, job_type, payload) values (%s,%s,%s)"
            " returning id",
            (TEAM_A, job_type, Json(payload or {})),
        ).fetchone()[0]
    finally:
        conn.close()


def _status(job_id):
    conn = _admin()
    try:
        return conn.execute(
            "select status, attempts, last_error from public.jobs where id=%s",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()


def test_empty_queue_returns_false(seeded):
    _clear_jobs()
    assert worker.run_once(handlers={}) is False


def test_job_is_claimed_and_completed(seeded):
    _clear_jobs()
    seen = []
    jid = _enqueue(payload={"document_id": "doc-1"})

    def handler(team_id, payload):
        seen.append((team_id, payload["document_id"]))

    assert worker.run_once(handlers={"parse_document": handler}) is True
    assert seen == [(TEAM_A, "doc-1")]
    status, attempts, err = _status(jid)
    assert status == "done" and attempts == 1 and err is None


def test_failing_job_retries_then_fails(seeded):
    _clear_jobs()
    jid = _enqueue()

    def boom(team_id, payload):
        raise RuntimeError("kaboom")

    handlers = {"parse_document": boom}
    for _ in range(worker.MAX_ATTEMPTS):
        worker.run_once(handlers=handlers)

    status, attempts, err = _status(jid)
    assert status == "failed"
    assert attempts == worker.MAX_ATTEMPTS
    assert "kaboom" in err


def test_skip_locked_prevents_double_claim(seeded):
    _clear_jobs()
    a = _enqueue(payload={"document_id": "A"})
    b = _enqueue(payload={"document_id": "B"})

    # hold a claim open (uncommitted) so its row is locked
    holder = psycopg.connect(settings.comrade_db_url_admin)
    try:
        first = holder.execute(worker._CLAIM_SQL).fetchone()  # in a transaction
        # a second claimer must SKIP the locked row and get the other job
        other = psycopg.connect(settings.comrade_db_url_admin)
        other.autocommit = True
        try:
            second = other.execute(worker._CLAIM_SQL).fetchone()
        finally:
            other.close()
        assert first is not None and second is not None
        assert first[0] != second[0]                 # different job ids
        assert {a, b} == {first[0], second[0]}
    finally:
        holder.rollback()
        holder.close()
