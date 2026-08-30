"""A compiled document keeps its extracted text.

findings §3.1: `documents.parsed_text` is missing, so extracted text lives only
in a transient job payload (`compiler.py`). Once a document is compiled its
text is gone — and `document_read` would have nothing to open.
"""
import psycopg
import pytest

from pipeline.compiler import handle_document_job
from shared.config import settings
from tests._seed import TEAM_A


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def _document(admin, **over):
    fields = {"kind": "text", "filename": "plan.txt"}
    fields.update(over)
    return admin.execute(
        "insert into public.documents (team_id, kind, filename)"
        " values (%s,%s,%s) returning id",
        (TEAM_A, fields["kind"], fields["filename"]),
    ).fetchone()[0]


def test_documents_have_a_parsed_text_column(admin):
    col = admin.execute(
        "select data_type from information_schema.columns"
        " where table_schema='public' and table_name='documents'"
        " and column_name='parsed_text'"
    ).fetchone()
    assert col is not None and col[0] == "text"


def test_a_compiled_document_keeps_its_text(seeded, admin, monkeypatch):
    """The compile is stubbed: this test is about persistence, not extraction."""
    monkeypatch.setattr(
        "pipeline.compiler.compile_document", lambda *a, **k: {"added": 0}
    )
    doc_id = _document(admin)
    body = "The demo is on Friday and Maya owns the deck."

    handle_document_job(TEAM_A, {"document_id": str(doc_id), "kind": "text",
                                "content": body})

    row = admin.execute(
        "select status, parsed_text from public.documents where id=%s", (doc_id,)
    ).fetchone()
    assert row[0] == "ready"
    assert row[1] == body


def test_a_failed_parse_stores_no_text(seeded, admin):
    """Below MIN_PARSE_CHARS the job fails loudly; nothing is persisted."""
    from pipeline.worker import PermanentJobError

    doc_id = _document(admin)
    with pytest.raises(PermanentJobError):
        handle_document_job(TEAM_A, {"document_id": str(doc_id), "kind": "text",
                                     "content": "tiny"})

    row = admin.execute(
        "select status, parsed_text from public.documents where id=%s", (doc_id,)
    ).fetchone()
    assert row[0] == "failed"
    assert row[1] is None


def test_the_messages_full_text_index_exists(admin):
    """messages_search (Task 3) is unbuildable without it."""
    idx = admin.execute(
        "select indexdef from pg_indexes"
        " where schemaname='public' and tablename='messages'"
        " and indexname='idx_messages_fts'"
    ).fetchone()
    assert idx is not None, "idx_messages_fts is missing"
    assert "gin" in idx[0].lower()
    assert "to_tsvector" in idx[0].lower()
