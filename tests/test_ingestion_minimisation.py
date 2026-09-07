"""What a queued document job carries, and who can read it.

🔴 THE DEFECT. `enqueue_document` put the WHOLE DOCUMENT in `jobs.payload` —
base64 for pdf and docx, raw text otherwise — and `comrade_control` holds
`select (… payload …)` on that table. The control plane is the cross-team
maintenance role: it claims jobs, sweeps queues, reaps leases. It could read
the full contents of every document every team had ever uploaded, because the
queue was carrying them.

The file was already in private Storage by then. The job only ever needed to
say WHICH file.

`handle_document_job`'s own docstring had it queued: "Production will fetch
bytes from Supabase Storage by the document's storage_path instead."
"""
import base64
import hashlib

import psycopg
import pytest

from shared.config import settings
from tests._seed import A1, TEAM_A


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _document(kind="text", storage_path="team/report.txt", deleted=False):
    conn = _admin()
    try:
        row = conn.execute(
            "insert into public.documents (team_id, uploader_id, kind, filename,"
            " storage_path, status, deleted_at) values (%s,%s,%s,'report.txt',%s,"
            "'uploaded', %s) returning id",
            (TEAM_A, A1, kind, storage_path,
             "now()" if False else (None if not deleted else "2026-01-01")),
        ).fetchone()
        return str(row[0])
    finally:
        conn.close()


def _payload(job_id):
    conn = _admin()
    try:
        return conn.execute(
            "select payload from public.jobs where id=%s", (job_id,)
        ).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The queue carries a reference, not the bytes
# ---------------------------------------------------------------------------

def test_a_queued_document_job_does_not_carry_the_document(seeded):
    """🔴 It carried the whole thing, and comrade_control can read payloads."""
    from pipeline.compiler import enqueue_document

    document_id = _document()

    job_id = enqueue_document(
        TEAM_A, document_id, "text", storage_path="team/report.txt",
    )

    payload = _payload(job_id)
    assert payload["storage_path"] == "team/report.txt"
    assert "content" not in payload, "the bytes are still in the queue"


def test_the_job_records_which_bytes_it_meant(seeded):
    """Content identity, so a job cannot silently parse a file that was
    replaced at the same path between queueing and running."""
    from pipeline.compiler import enqueue_document

    document_id = _document()
    digest = hashlib.sha256(b"the deadline is Friday").hexdigest()

    job_id = enqueue_document(
        TEAM_A, document_id, "text", storage_path="team/report.txt",
        content_sha256=digest,
    )

    assert _payload(job_id)["content_sha256"] == digest


def test_the_control_role_can_still_run_the_queue_without_reading_documents(seeded):
    """The point of the change: the maintenance role keeps its job, and loses
    the ability to read a team's files while doing it."""
    from pipeline.compiler import enqueue_document
    from shared.db import Role, connect

    document_id = _document()
    enqueue_document(TEAM_A, document_id, "text", storage_path="team/report.txt")

    with connect(Role.CONTROL) as conn:
        conn.autocommit = True
        payloads = conn.execute(
            "select payload from public.jobs where job_type='parse_document'"
            " and team_id=%s",
            (TEAM_A,),
        ).fetchall()

    # The seed leaves one parse_document job with a NULL payload; a row that
    # carries nothing obviously carries no document.
    assert payloads, "no jobs to check"
    for (payload,) in payloads:
        assert not payload or "content" not in payload


# ---------------------------------------------------------------------------
# Fetching, with limits, under the worker's own permission
# ---------------------------------------------------------------------------

def test_the_worker_fetches_the_file_it_was_pointed_at(seeded, monkeypatch):
    from pipeline import compiler

    document_id = _document()
    fetched: list[str] = []
    monkeypatch.setattr(
        compiler, "download_document",
        lambda path, **_: fetched.append(path) or b"the deadline is Friday, and it matters",
    )
    monkeypatch.setattr(compiler, "compile_document", lambda *a, **k: {"added": 1})

    compiler.handle_document_job(
        TEAM_A, {"document_id": document_id, "kind": "text",
                 "storage_path": "team/report.txt"},
    )

    assert fetched == ["team/report.txt"]


def test_an_inline_payload_still_works_during_a_deploy(seeded, monkeypatch):
    """Jobs queued by the previous image carry `content`. They have to keep
    running, or a release strands whatever was in the queue when it started."""
    from pipeline import compiler

    document_id = _document()
    monkeypatch.setattr(compiler, "compile_document", lambda *a, **k: {"added": 1})

    compiler.handle_document_job(
        TEAM_A,
        {"document_id": document_id, "kind": "text",
         "content": "the deadline is Friday, and it matters"},
    )


def test_a_document_deleted_before_the_parse_is_not_parsed(seeded, monkeypatch):
    """🔴 Nothing rechecked. A member could delete a document and its contents
    would still be fetched, parsed and compiled into the wiki afterwards."""
    from pipeline import compiler

    document_id = _document()
    conn = _admin()
    try:
        conn.execute(
            "update public.documents set deleted_at=now() where id=%s",
            (document_id,),
        )
    finally:
        conn.close()
    monkeypatch.setattr(
        compiler, "download_document",
        lambda *a, **k: pytest.fail("a deleted document must not be fetched"),
    )

    with pytest.raises(compiler.PermanentJobError):
        compiler.handle_document_job(
            TEAM_A, {"document_id": document_id, "kind": "text",
                     "storage_path": "team/report.txt"},
        )


def test_a_document_deleted_mid_job_is_not_compiled(seeded, monkeypatch):
    """Deletion during a parse is the interesting case: the fetch succeeded,
    and the member has since said they did not want this in the system."""
    from pipeline import compiler

    document_id = _document()

    def _download(*_a, **_k):
        conn = _admin()
        try:
            conn.execute(
                "update public.documents set deleted_at=now() where id=%s",
                (document_id,),
            )
        finally:
            conn.close()
        return b"the deadline is Friday, and it matters"

    monkeypatch.setattr(compiler, "download_document", _download)
    monkeypatch.setattr(
        compiler, "compile_document",
        lambda *a, **k: pytest.fail("a deleted document must not be compiled"),
    )

    with pytest.raises(compiler.PermanentJobError):
        compiler.handle_document_job(
            TEAM_A, {"document_id": document_id, "kind": "text",
                     "storage_path": "team/report.txt"},
        )


def test_an_oversized_file_is_refused_before_it_is_parsed(seeded, monkeypatch):
    """🔴 Nothing bounded this. A parser allocating a gigabyte is the worker
    dying, and every other team's jobs waiting behind the restart."""
    from pipeline import compiler

    document_id = _document()
    monkeypatch.setattr(compiler, "MAX_DOCUMENT_BYTES", 64)
    monkeypatch.setattr(
        compiler, "download_document",
        lambda *a, **k: (_ for _ in ()).throw(
            compiler.DocumentTooLarge("file is 5 MB, limit is 64 bytes")
        ),
    )

    with pytest.raises(compiler.PermanentJobError):
        compiler.handle_document_job(
            TEAM_A, {"document_id": document_id, "kind": "text",
                     "storage_path": "team/report.txt"},
        )


def test_a_legacy_doc_is_refused_rather_than_parsed_as_docx(seeded, monkeypatch):
    """🔴 The uploader maps .doc to kind 'docx'. A legacy .doc is an OLE
    compound file that python-docx cannot read at all, so it uploaded, parsed
    to nothing, and came back as "likely scanned" — which is a wrong
    explanation, and the member cannot act on it."""
    from pipeline import compiler

    conn = _admin()
    try:
        document_id = str(conn.execute(
            "insert into public.documents (team_id, uploader_id, kind, filename,"
            " storage_path, status) values (%s,%s,'docx','old.doc','team/old.doc',"
            "'uploaded') returning id",
            (TEAM_A, A1),
        ).fetchone()[0])
    finally:
        conn.close()
    monkeypatch.setattr(
        compiler, "download_document",
        lambda *a, **k: pytest.fail("an unsupported format must not be fetched"),
    )

    with pytest.raises(compiler.PermanentJobError) as caught:
        compiler.handle_document_job(
            TEAM_A, {"document_id": document_id, "kind": "docx",
                     "filename": "old.doc", "storage_path": "team/old.doc"},
        )

    assert ".doc" in str(caught.value)


def test_a_failure_reason_is_written_where_the_member_can_see_it(seeded, monkeypatch):
    from pipeline import compiler

    document_id = _document()
    monkeypatch.setattr(
        compiler, "download_document",
        lambda *a, **k: (_ for _ in ()).throw(
            compiler.DocumentTooLarge("file is 5 MB")
        ),
    )

    with pytest.raises(compiler.PermanentJobError):
        compiler.handle_document_job(
            TEAM_A, {"document_id": document_id, "kind": "text",
                     "storage_path": "team/report.txt"},
        )

    conn = _admin()
    try:
        status, reason = conn.execute(
            "select status, parse_error from public.documents where id=%s",
            (document_id,),
        ).fetchone()
    finally:
        conn.close()
    assert status == "failed"
    assert reason and "5 MB" in reason
