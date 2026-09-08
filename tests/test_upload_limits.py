"""How much of an upload the API will hold before saying no.

🔴 THE DEFECT (fix.md F23). The 25 MiB cap covered the worker's DOWNLOAD —
the path where the bytes were already in private Storage and already known to
be fine — and neither of the API's reads, which are the ones a caller controls.

`hashlib.sha256(file.file.read())` allocated an entire upload purely to compute
an identity it then discarded. The legacy inline path did `file.file.read()`
and put the result in a job payload as base64, a third larger again. And the
worker's inline branch parsed whatever it was handed with no size check at all,
so an oversized legacy job reached the parser and the model.

Measuring after `read()` is measuring after the allocation has already
happened, which is the thing a cap exists to prevent.
"""
import io

import pytest

from shared.storage import (
    MAX_DOCUMENT_BYTES, DocumentTooLarge, hash_upload, read_upload,
)


class _CountingStream(io.BytesIO):
    """Reports how much was actually pulled out of it."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.served = 0

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        chunk = super().read(size)
        self.served += len(chunk)
        return chunk


# ---------------------------------------------------------------------------

def test_hashing_an_oversized_upload_is_refused():
    """🔴 It was hashed in full. The identity is not worth the allocation."""
    stream = io.BytesIO(b"x" * (MAX_DOCUMENT_BYTES + 1024))

    with pytest.raises(DocumentTooLarge):
        hash_upload(stream)


def test_hashing_stops_early_rather_than_reading_it_all():
    """The bound has to apply to what is READ, not to what is returned — a
    check after the fact protects nothing."""
    oversize = MAX_DOCUMENT_BYTES * 3
    stream = _CountingStream(b"x" * oversize)

    with pytest.raises(DocumentTooLarge):
        hash_upload(stream)

    assert stream.served < oversize, (
        f"the whole {oversize:,} bytes were pulled in before refusing"
    )


def test_reading_an_oversized_upload_is_refused():
    stream = io.BytesIO(b"x" * (MAX_DOCUMENT_BYTES + 1024))

    with pytest.raises(DocumentTooLarge):
        read_upload(stream)


def test_reading_stops_early_too():
    oversize = MAX_DOCUMENT_BYTES * 3
    stream = _CountingStream(b"x" * oversize)

    with pytest.raises(DocumentTooLarge):
        read_upload(stream)

    assert stream.served < oversize


def test_a_permitted_upload_hashes_to_the_same_thing_as_before():
    """The bound must not change the answer for anything it allows — the
    digest is compared against one the worker computes over the stored file."""
    import hashlib

    payload = b"the reviewed handbook\n" * 1000
    expected = hashlib.sha256(payload).hexdigest()

    assert hash_upload(io.BytesIO(payload)) == expected


def test_a_permitted_upload_reads_back_intact():
    payload = bytes(range(256)) * 500

    assert read_upload(io.BytesIO(payload)) == payload


def test_an_empty_upload_is_not_an_error():
    """Zero bytes is a real thing to upload and a strange thing to refuse."""
    assert read_upload(io.BytesIO(b"")) == b""


def test_the_cap_is_the_documented_one():
    """One number, in one place. A second limit that disagreed would be worse
    than the missing one."""
    assert MAX_DOCUMENT_BYTES == 25 * 1024 * 1024


# ---------------------------------------------------------------------------
# The worker's legacy path
# ---------------------------------------------------------------------------

def test_an_oversized_legacy_job_fails_before_parsing(seeded, monkeypatch):
    """🔴 A job queued by an older image carries its content inline, and
    nothing measured it before parsing — so the one path whose size was never
    checked at the API was also the one the worker did not check."""
    import psycopg

    from pipeline import compiler
    from shared.config import settings
    from tests._seed import A1, TEAM_A

    parsed = {"called": False}
    monkeypatch.setattr(
        compiler, "_parse_by_kind",
        lambda kind, content: parsed.__setitem__("called", True) or "text",
    )

    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        document_id = str(conn.execute(
            "insert into public.documents (team_id, uploader_id, kind,"
            " filename, status) values (%s,%s,'text','big.txt','uploaded')"
            " returning id", (TEAM_A, A1),
        ).fetchone()[0])
    finally:
        conn.close()

    with pytest.raises(compiler.PermanentJobError):
        compiler.handle_document_job(TEAM_A, {
            "document_id": document_id, "kind": "text",
            "content": "x" * (MAX_DOCUMENT_BYTES + 1),
        })

    assert parsed["called"] is False, "the parser ran on an oversized payload"
