"""Whether the bytes compiled are the bytes that were queued.

🔴 THE DEFECT (fix.md F22). `content_sha256` was recorded into the job payload
at enqueue and never compared with anything — the field's own docstring said it
"records which bytes were meant", and nothing ever asked.

T21 moved the document bytes out of the payload and left a REFERENCE in their
place, which is right: the queue stopped carrying team content past a role that
should not see it. But a reference is only as good as the check that it still
points at what was meant. Replace the object at the same storage path between
enqueue and fetch and a different file was compiled into the team wiki, under
the citation of the one somebody had reviewed.
"""
import hashlib

import psycopg
import pytest

from pipeline import compiler
from shared.config import settings
from tests._seed import A1, TEAM_A

ORIGINAL = b"the reviewed version of the handbook\n"
SWAPPED = b"the version nobody looked at\n"


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _document() -> tuple[str, str]:
    conn = _admin()
    try:
        path = f"{TEAM_A}/handbook.md"
        document_id = conn.execute(
            "insert into public.documents (team_id, uploader_id, kind,"
            " filename, storage_path, status) values"
            " (%s,%s,'text','handbook.md',%s,'uploaded') returning id",
            (TEAM_A, A1, path),
        ).fetchone()[0]
    finally:
        conn.close()
    return str(document_id), path


def _row(document_id: str) -> tuple:
    conn = _admin()
    try:
        return conn.execute(
            "select status, parse_error, content_sha256, content_verified"
            "  from public.documents where id=%s", (document_id,),
        ).fetchone()
    finally:
        conn.close()


@pytest.fixture
def served(monkeypatch):
    """Whatever the store hands back, whether or not it is what was queued."""
    box = {"bytes": ORIGINAL}
    monkeypatch.setattr(
        compiler, "download_document",
        lambda path, max_bytes=None: box["bytes"],
    )
    # The compile itself is not what these tests are about.
    monkeypatch.setattr(compiler, "compile_document", lambda *a, **k: None)
    return box


# ---------------------------------------------------------------------------

def test_a_swapped_file_is_refused(seeded, served):
    """🔴 It was compiled. Same path, different bytes, no complaint."""
    document_id, path = _document()
    served["bytes"] = SWAPPED

    with pytest.raises(compiler.PermanentJobError):
        compiler.handle_document_job(TEAM_A, {
            "document_id": document_id, "kind": "text", "storage_path": path,
            "content_sha256": hashlib.sha256(ORIGINAL).hexdigest(),
        })

    status, error, _, _ = _row(document_id)
    assert status == "failed"
    assert "changed after this document was queued" in error


def test_a_swapped_file_is_not_retried(seeded, served):
    """`PermanentJobError`, not a transient one: the bytes this job was about
    are gone, and asking again cannot bring them back."""
    document_id, path = _document()
    served["bytes"] = SWAPPED

    with pytest.raises(compiler.PermanentJobError):
        compiler.handle_document_job(TEAM_A, {
            "document_id": document_id, "kind": "text", "storage_path": path,
            "content_sha256": hashlib.sha256(ORIGINAL).hexdigest(),
        })


def test_unchanged_bytes_compile_and_are_marked_verified(seeded, served):
    document_id, path = _document()

    compiler.handle_document_job(TEAM_A, {
        "document_id": document_id, "kind": "text", "storage_path": path,
        "content_sha256": hashlib.sha256(ORIGINAL).hexdigest(),
    })

    _, _, digest, verified = _row(document_id)
    assert digest == hashlib.sha256(ORIGINAL).hexdigest()
    assert verified is True


def test_a_job_with_no_expectation_is_recorded_but_not_called_verified(seeded, served):
    """Compatibility, stated honestly. A job enqueued before this existed has
    nothing to compare against — recording the observed hash without saying so
    would imply a check that never happened."""
    document_id, path = _document()

    compiler.handle_document_job(TEAM_A, {
        "document_id": document_id, "kind": "text", "storage_path": path,
    })

    _, _, digest, verified = _row(document_id)
    assert digest == hashlib.sha256(ORIGINAL).hexdigest()
    assert verified is False, (
        "an unchecked job was recorded as if its source had been verified"
    )
