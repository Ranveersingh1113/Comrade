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
    conn = _admin()
    try:
        for _ in range(worker.MAX_ATTEMPTS):
            worker.run_once(handlers=handlers)
            # A retry now waits before it is claimable (T12): burning three
            # attempts in a few milliseconds against a thing that has had no
            # time to change is not retrying. Skip the wait rather than sleep
            # through it — the backoff itself is tested in
            # tests/test_pipeline_leases.py.
            conn.execute(
                "update public.jobs set available_at = now() - interval '1 second'"
                " where id=%s",
                (jid,),
            )
    finally:
        conn.close()

    status, attempts, err = _status(jid)
    assert status == "failed"
    assert attempts == worker.MAX_ATTEMPTS
    assert "kaboom" in err


def test_permanent_failure_does_not_retry(seeded):
    _clear_jobs()
    jid = _enqueue()

    def bad_payload(team_id, payload):
        raise worker.PermanentJobError("unsupported input")

    assert worker.run_once(handlers={"parse_document": bad_payload}) is True
    status, attempts, err = _status(jid)
    assert (status, attempts, err) == ("failed", 1, "unsupported input")


def test_expired_lease_is_reclaimed(seeded):
    _clear_jobs()
    jid = _enqueue()
    conn = _admin()
    try:
        conn.execute(
            "update public.jobs set status='processing', attempts=1,"
            " lease_expires_at=now() - interval '1 minute' where id=%s",
            (jid,),
        )
    finally:
        conn.close()

    assert worker.run_once(handlers={"parse_document": lambda *_: None}) is True
    status, attempts, err = _status(jid)
    assert status == "done" and attempts == 2 and err is None


def test_skip_locked_prevents_double_claim(seeded):
    _clear_jobs()
    a = _enqueue(payload={"document_id": "A"})
    b = _enqueue(payload={"document_id": "B"})

    # hold a claim open (uncommitted) so its row is locked
    holder = psycopg.connect(settings.comrade_db_url_admin)
    try:
        first = holder.execute(
            worker._CLAIM_SQL, ("worker-one",)
        ).fetchone()  # in a transaction
        # a second claimer must SKIP the locked row and get the other job
        other = psycopg.connect(settings.comrade_db_url_admin)
        other.autocommit = True
        try:
            second = other.execute(worker._CLAIM_SQL, ("worker-two",)).fetchone()
        finally:
            other.close()
        assert first is not None and second is not None
        assert first[0] != second[0]                 # different job ids
        assert {a, b} == {first[0], second[0]}
    finally:
        holder.rollback()
        holder.close()


def test_empty_parse_marks_document_failed(seeded):
    """Scanned/empty documents must fail loudly, not compile silently."""
    import psycopg
    import pytest as _pytest

    from pipeline.compiler import handle_document_job
    from shared.config import settings as _settings
    from tests._seed import TEAM_A as _TEAM_A

    conn = psycopg.connect(_settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        doc_id = conn.execute(
            "insert into public.documents (team_id, kind, filename)"
            " values (%s,'text','blank.txt') returning id",
            (_TEAM_A,),
        ).fetchone()[0]
        with _pytest.raises(ValueError, match="parsed to"):
            handle_document_job(
                _TEAM_A, {"document_id": str(doc_id), "kind": "text", "content": "   "}
            )
        status = conn.execute(
            "select status from public.documents where id=%s", (doc_id,)
        ).fetchone()[0]
        assert status == "failed"
    finally:
        conn.close()


def test_tick_drains_queue_then_sweeps(seeded, monkeypatch):
    _clear_jobs()
    _enqueue(payload={"document_id": "A"})
    _enqueue(payload={"document_id": "B"})

    swept = []
    monkeypatch.setattr(
        "pipeline.chat.sweep_chat_compiles", lambda: swept.append(1) or []
    )
    handled = []
    original = worker._HANDLERS.get("parse_document")
    worker.register("parse_document", lambda t, p: handled.append(p))
    try:
        assert worker.tick() == 2
    finally:
        if original is not None:
            worker._HANDLERS["parse_document"] = original
        else:
            worker._HANDLERS.pop("parse_document", None)
    assert len(handled) == 2 and swept == [1]


def test_tick_survives_sweep_failure(seeded, monkeypatch):
    _clear_jobs()

    def boom():
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr("pipeline.chat.sweep_chat_compiles", boom)
    assert worker.tick() == 0  # no crash — drain path unaffected
