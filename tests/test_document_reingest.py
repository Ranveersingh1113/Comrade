"""Retrying an ingestion that failed, without asking for the file again.

🔴 THE DEFECT. `POST /documents/{id}/ingest` takes the bytes as multipart, so
the only way to retry a failed parse was to upload the whole file a second
time — and the browser had thrown the File away on the first reload. A member
whose ingestion failed was told what went wrong and offered nothing to do
about it except find the document again and re-upload a file the system was
already storing.

The ingest endpoint's own docstring called this out: "Fetching by storage_path
instead is a later slice that removes the double upload."
"""
import psycopg
import pytest
from fastapi.testclient import TestClient

from server.app import app
from server.auth import current_user_id
from shared.config import settings
from tests._seed import A1, B1, TEAM_A


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _client(user_id: str) -> TestClient:
    app.dependency_overrides[current_user_id] = lambda: user_id
    return TestClient(app)


def _document(*, storage_path: str | None = "team/report.md", kind: str = "text") -> str:
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        row = conn.execute(
            "insert into public.documents (team_id, uploader_id, kind, filename,"
            " storage_path, status) values (%s,%s,%s,'report.md',%s,'failed')"
            " returning id",
            (TEAM_A, A1, kind, storage_path),
        ).fetchone()
        conn.commit()
    return str(row[0])


def test_a_failed_document_can_be_retried_from_what_is_already_stored(
    seeded, monkeypatch,
):
    """T21 moved the FETCH to the worker.

    This endpoint used to download the file and put the bytes into the job
    payload — a table `comrade_control` can read — so it queues a REFERENCE
    now and the worker reads the file under its own permission when it is
    ready to parse it.
    """
    document_id = _document()
    queued: list[dict] = []
    monkeypatch.setattr(
        "server.app.enqueue_document",
        lambda *args, **kwargs: queued.append({"args": args, "kwargs": kwargs})
        or "job-1",
    )

    resp = _client(A1).post(
        f"/documents/{document_id}/reingest", json={"team_id": TEAM_A},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["job_id"] == "job-1"
    assert queued and queued[0]["args"][1] == document_id
    assert queued[0]["kwargs"]["storage_path"] == "team/report.md"


def test_a_document_with_nothing_stored_says_so_instead_of_failing_oddly(seeded):
    """Some rows predate storage, and "retry" cannot mean anything for them."""
    document_id = _document(storage_path=None)

    resp = _client(A1).post(
        f"/documents/{document_id}/reingest", json={"team_id": TEAM_A},
    )

    assert resp.status_code == 409, resp.text
    assert "upload" in resp.json()["detail"].lower()


def test_another_team_cannot_retry_a_document_it_cannot_see(seeded):
    document_id = _document()

    resp = _client(B1).post(
        f"/documents/{document_id}/reingest", json={"team_id": TEAM_A},
    )

    assert resp.status_code == 403, resp.text


def test_a_missing_document_is_a_404(seeded):
    resp = _client(A1).post(
        "/documents/11111111-1111-1111-1111-111111111111/reingest",
        json={"team_id": TEAM_A},
    )

    assert resp.status_code == 404, resp.text


def test_binary_documents_are_carried_the_same_way_on_both_paths(
    seeded, monkeypatch,
):
    """A pdf that arrives base64 on one path and raw on the other is a parser
    bug waiting for whichever path is used second.

    The encoding moved with the fetch. T21 made the WORKER read stored bytes
    and turn them into what the parser expects, so that is where this belongs
    now — the endpoint no longer touches the file at all.
    """
    import base64

    from pipeline import compiler

    document_id = _document(kind="pdf")
    monkeypatch.setattr(
        compiler, "download_document", lambda *a, **k: b"\x00\x01binary",
    )
    seen: list[str] = []
    monkeypatch.setattr(
        compiler, "_parse_by_kind",
        lambda kind, content: seen.append(content) or ("x" * 200),
    )
    monkeypatch.setattr(compiler, "compile_document", lambda *a, **k: {"added": 0})

    compiler.handle_document_job(
        TEAM_A,
        {"document_id": document_id, "kind": "pdf",
         "storage_path": "team/report.md"},
    )

    assert seen == [base64.b64encode(b"\x00\x01binary").decode("ascii")]
