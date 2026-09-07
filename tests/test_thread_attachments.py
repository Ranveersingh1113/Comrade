"""A file attached to a thread, and what the system is allowed to do with it.

🔴 THE DEFECT. Every uploaded document was TEAM KNOWLEDGE the moment it
landed. There was no thread binding and no purpose, and the compiler ingested
whatever was uploaded straight into the team wiki — which every member can
read. So a file dropped into a restricted thread would have had its contents
published to people who cannot open that thread: not by a bug in the compiler,
but by the compiler working exactly as designed on a document nobody had said
was shareable.

The default is now the narrow one. A member dropping a file into a
conversation is not publishing it.
"""
import psycopg
import pytest

from shared.config import settings
from tests._seed import A1, A2, B1, TEAM_A, as_user


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _thread(conn, visibility="restricted", title="Security review"):
    thread_id = str(conn.execute(
        "insert into public.threads (team_id, title, visibility, kind, created_by)"
        " values (%s,%s,%s,'discussion',%s) returning id",
        (TEAM_A, title, visibility, A1),
    ).fetchone()[0])
    if visibility == "restricted":
        conn.execute(
            "insert into public.thread_participants (thread_id, team_id, user_id,"
            " added_by) values (%s,%s,%s,%s)",
            (thread_id, TEAM_A, A1, A1),
        )
    return thread_id


def _document(conn, *, thread_id=None, purpose=None, filename="notes.txt"):
    if purpose is None:
        return str(conn.execute(
            "insert into public.documents (team_id, uploader_id, kind, filename,"
            " storage_path, status, thread_id)"
            " values (%s,%s,'text',%s,'team/notes.txt','uploaded',%s) returning id",
            (TEAM_A, A1, filename, thread_id),
        ).fetchone()[0])
    return str(conn.execute(
        "insert into public.documents (team_id, uploader_id, kind, filename,"
        " storage_path, status, thread_id, purpose)"
        " values (%s,%s,'text',%s,'team/notes.txt','uploaded',%s,%s) returning id",
        (TEAM_A, A1, filename, thread_id, purpose),
    ).fetchone()[0])


# ---------------------------------------------------------------------------
# The default is the narrow one
# ---------------------------------------------------------------------------

def test_an_attachment_defaults_to_this_turn_only(seeded):
    """🔴 Everything used to be team knowledge on arrival. A member dropping a
    file into a conversation is not publishing it."""
    conn = _admin()
    try:
        thread_id = _thread(conn)
        document_id = _document(conn, thread_id=thread_id)
        purpose = conn.execute(
            "select purpose from public.documents where id=%s", (document_id,)
        ).fetchone()[0]
    finally:
        conn.close()

    assert purpose == "turn_context"


def test_documents_that_predate_purposes_stay_team_knowledge(seeded):
    """They were uploaded through the team screen and compiled into the wiki,
    so that is what they are. Calling them turn_context would claim a privacy
    they never actually had."""
    conn = _admin()
    try:
        rows = conn.execute(
            "select count(*) from public.documents"
            " where thread_id is null and purpose <> 'team_knowledge'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert rows == 0


# ---------------------------------------------------------------------------
# Restricted attachments stay restricted
# ---------------------------------------------------------------------------

def test_an_attachment_in_a_restricted_thread_is_invisible_to_others(seeded):
    conn = _admin()
    try:
        thread_id = _thread(conn)
        _document(conn, thread_id=thread_id, filename="leaked-key.txt")
    finally:
        conn.close()

    with as_user(A2) as conn:
        rows = conn.execute(
            "select filename from public.documents where team_id=%s", (TEAM_A,)
        ).fetchall()

    assert not any(r[0] == "leaked-key.txt" for r in rows)


def test_a_participant_sees_it(seeded):
    conn = _admin()
    try:
        thread_id = _thread(conn)
        _document(conn, thread_id=thread_id, filename="leaked-key.txt")
    finally:
        conn.close()

    with as_user(A1) as conn:
        rows = conn.execute(
            "select filename from public.documents where team_id=%s", (TEAM_A,)
        ).fetchall()

    assert any(r[0] == "leaked-key.txt" for r in rows)


def test_a_team_document_is_still_visible_to_the_team(seeded):
    conn = _admin()
    try:
        _document(conn, thread_id=None, purpose="team_knowledge",
                  filename="handbook.txt")
    finally:
        conn.close()

    with as_user(A2) as conn:
        rows = conn.execute(
            "select filename from public.documents where team_id=%s", (TEAM_A,)
        ).fetchall()

    assert any(r[0] == "handbook.txt" for r in rows)


def test_another_team_sees_none_of_it(seeded):
    conn = _admin()
    try:
        _document(conn, thread_id=None, purpose="team_knowledge")
    finally:
        conn.close()

    with as_user(B1) as conn:
        rows = conn.execute(
            "select 1 from public.documents where team_id=%s", (TEAM_A,)
        ).fetchall()

    assert rows == []


# ---------------------------------------------------------------------------
# Nothing is compiled into the wiki without a member saying so
# ---------------------------------------------------------------------------

def test_the_compiler_refuses_a_document_that_is_not_team_knowledge(seeded):
    """🔴 The compiler put whatever it was given into the TEAM wiki. A file
    attached to a restricted thread would have had its contents published to
    everyone, by the compiler working exactly as designed."""
    from pipeline.compiler import enqueue_document
    from pipeline.worker import PermanentJobError

    conn = _admin()
    try:
        thread_id = _thread(conn)
        document_id = _document(conn, thread_id=thread_id)
    finally:
        conn.close()

    with pytest.raises(PermanentJobError):
        enqueue_document(
            TEAM_A, document_id, "text", storage_path="team/notes.txt",
        )


def test_a_promoted_document_compiles(seeded, monkeypatch):
    from pipeline.compiler import enqueue_document

    conn = _admin()
    try:
        thread_id = _thread(conn)
        document_id = _document(conn, thread_id=thread_id, purpose="team_knowledge")
    finally:
        conn.close()

    job_id = enqueue_document(
        TEAM_A, document_id, "text", storage_path="team/notes.txt",
    )
    assert job_id


def test_a_plain_team_document_still_compiles(seeded):
    from pipeline.compiler import enqueue_document

    conn = _admin()
    try:
        document_id = _document(conn, thread_id=None, purpose="team_knowledge")
    finally:
        conn.close()

    assert enqueue_document(
        TEAM_A, document_id, "text", storage_path="team/notes.txt",
    )


# ---------------------------------------------------------------------------
# Promotion is recorded
# ---------------------------------------------------------------------------

def test_promoting_an_attachment_is_written_down(seeded):
    """A file becoming readable by the whole team is a decision somebody made."""
    conn = _admin()
    try:
        thread_id = _thread(conn)
        document_id = _document(conn, thread_id=thread_id)
    finally:
        conn.close()

    with as_user(A1, commit=True) as conn:
        conn.execute(
            "update public.documents set purpose='team_knowledge' where id=%s",
            (document_id,),
        )

    conn = _admin()
    try:
        rows = conn.execute(
            "select from_purpose, to_purpose, actor_id::text"
            " from public.document_promotions where document_id=%s",
            (document_id,),
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("turn_context", "team_knowledge", A1)]


def test_a_non_participant_cannot_promote_someone_elses_attachment(seeded):
    conn = _admin()
    try:
        thread_id = _thread(conn)
        document_id = _document(conn, thread_id=thread_id)
    finally:
        conn.close()

    with as_user(A2, commit=True) as conn:
        conn.execute(
            "update public.documents set purpose='team_knowledge' where id=%s",
            (document_id,),
        )

    conn = _admin()
    try:
        purpose = conn.execute(
            "select purpose from public.documents where id=%s", (document_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert purpose == "turn_context", "RLS must refuse the update, not apply it"
