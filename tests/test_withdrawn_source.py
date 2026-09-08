"""Deleting a document while Comrade is reading it.

🔴 THE DEFECT (fix.md F21). Deletion and purpose were checked before the work
began, and `compile_document` then makes TWO model calls — extraction, then
consolidation against the whole wiki — before it applies anything. That is
minutes, during which a member can delete the document or demote it from
`team_knowledge` to `turn_context` after seeing what it contains.

Neither stopped publication. The facts went into the team wiki anyway, because
the check that authorised the work ran in a different transaction from the
write it was supposed to guard: true when it was asked, false by the time it
mattered. The parse result had no deletion guard at all, so a deleted document
came back `ready` with its text stored — a deleted row restored to a live state
by a background job.
"""
import psycopg
import pytest

from pipeline import compiler
from shared.config import settings
from tests._seed import A1, TEAM_A


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _document(*, purpose: str = "team_knowledge") -> str:
    conn = _admin()
    try:
        return str(conn.execute(
            "insert into public.documents (team_id, uploader_id, kind,"
            " filename, storage_path, status, purpose) values"
            " (%s,%s,'text','handbook.md',%s,'uploaded',%s) returning id",
            (TEAM_A, A1, f"{TEAM_A}/handbook.md", purpose),
        ).fetchone()[0])
    finally:
        conn.close()


def _facts() -> int:
    conn = _admin()
    try:
        return conn.execute(
            "select count(*) from public.memory_entries where team_id=%s",
            (TEAM_A,),
        ).fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def model(monkeypatch):
    """Extraction and consolidation, with a seam to withdraw the source in."""
    hooks = {"between": lambda: None}

    def _extract(marked_text, kind="document"):
        return [compiler.Candidate(
            text="the deploy key rotates on Fridays",
            excerpt="the deploy key rotates on Fridays",
        )]

    def _consolidate(candidates, pages, length):
        # The window the defect lives in: between deciding what to publish and
        # publishing it.
        hooks["between"]()
        return [compiler.Decision(
            candidate_index=0, action="add", page_title="Operations",
            page_description="How this team runs things", page_kind="fact",
        )]

    monkeypatch.setattr(compiler, "extract_candidates", _extract)
    monkeypatch.setattr(compiler, "consolidate", _consolidate)
    return hooks


# ---------------------------------------------------------------------------

def test_deleting_a_document_mid_compile_publishes_nothing(seeded, model):
    """🔴 It published. The member deleted the file and its contents went into
    the team wiki regardless."""
    document_id = _document()
    before = _facts()

    def _delete():
        conn = _admin()
        try:
            conn.execute(
                "update public.documents set deleted_at=now(), deleted_by=%s"
                " where id=%s", (A1, document_id),
            )
        finally:
            conn.close()

    model["between"] = _delete

    with pytest.raises(compiler.SourceWithdrawn):
        compiler.compile_document(TEAM_A, document_id, "marked text")

    assert _facts() == before


def test_demoting_a_document_mid_compile_publishes_nothing(seeded, model):
    """The quieter half of the same race: not deleted, made private. T24 made
    `turn_context` mean "shown to the model for this work and nothing else",
    and a compile already in flight ignored it."""
    document_id = _document()
    before = _facts()

    def _demote():
        conn = _admin()
        try:
            conn.execute(
                "update public.documents set purpose='turn_context'"
                " where id=%s", (document_id,),
            )
        finally:
            conn.close()

    model["between"] = _demote

    with pytest.raises(compiler.SourceWithdrawn):
        compiler.compile_document(TEAM_A, document_id, "marked text")

    assert _facts() == before


def test_the_member_is_told_which_thing_happened(seeded, model):
    """Deleted and demoted are different actions with different explanations,
    and a member who did one should not be told about the other."""
    document_id = _document()
    model["between"] = lambda: None

    conn = _admin()
    try:
        conn.execute("update public.documents set purpose='turn_context'"
                     " where id=%s", (document_id,))
    finally:
        conn.close()

    with pytest.raises(compiler.SourceWithdrawn) as caught:
        compiler.compile_document(TEAM_A, document_id, "marked text")

    assert "private" in str(caught.value)


def test_a_document_nobody_touched_still_compiles(seeded, model):
    """The guard must not refuse ordinary work — that would be a quieter
    outage than the leak it replaces."""
    document_id = _document()
    before = _facts()

    compiler.compile_document(TEAM_A, document_id, "marked text")

    assert _facts() > before


def test_a_deleted_document_is_not_restored_to_ready(seeded, monkeypatch):
    """🔴 The parse result had no deletion guard, so a document deleted while
    it was being read came back `ready` with its text stored."""
    document_id = _document()
    monkeypatch.setattr(compiler, "download_document",
                        lambda path, max_bytes=None: b"a" * 500)
    monkeypatch.setattr(compiler, "compile_document", lambda *a, **k: None)

    conn = _admin()
    try:
        conn.execute("update public.documents set deleted_at=now(),"
                     " deleted_by=%s where id=%s", (A1, document_id))
    finally:
        conn.close()

    try:
        compiler.handle_document_job(TEAM_A, {
            "document_id": document_id, "kind": "text",
            "storage_path": f"{TEAM_A}/handbook.md",
        })
    except Exception:
        pass  # refusing outright is also an acceptable answer here

    conn = _admin()
    try:
        status, deleted = conn.execute(
            "select status, deleted_at is not null from public.documents"
            " where id=%s", (document_id,),
        ).fetchone()
    finally:
        conn.close()
    assert deleted is True
    assert status != "ready", "a deleted document was restored to a live state"
