"""Whose file the privileged reader is about to fetch.

🔴 THE DEFECT (fix.md F20). `documents.storage_path` is written by MEMBERS —
the frontend uploads to Storage and inserts the row itself under RLS — and
`shared/storage.py:download_document` fetches whatever path it is handed using
the Supabase SERVICE SECRET. Nothing checked that the object belonged to the
document, the team, or the thread.

The insert policy is `is_team_member(team_id) AND uploader_id = auth.uid()`.
It says nothing about `storage_path`. So a member could create a document row
in their own team pointing at another team's object, or at a restricted
same-team attachment they are not a participant of, and the pipeline would
read it with full privilege and compile its text into a wiki they can see.
The update policy allowed the same move in two steps: create with your own
file, then re-point.

Knowing the path is a prerequisite, not authority — and the same-team case
barely needs guessing, because the prefix is the team id the member already
has.
"""
import uuid

import psycopg
import pytest

from shared.config import settings
from tests._seed import A1, A2, B1, TEAM_A, TEAM_B, as_user


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _path(team_id: str, name: str = "secret.pdf") -> str:
    """The real convention: `<team id>/<uuid>-<filename>`."""
    return f"{team_id}/{uuid.uuid4()}-{name}"


def _document(team_id: str, uploader: str, path: str, *, thread_id=None) -> str:
    conn = _admin()
    try:
        return str(conn.execute(
            "insert into public.documents (team_id, uploader_id, kind,"
            " filename, storage_path, status, thread_id)"
            " values (%s,%s,'text','secret.pdf',%s,'uploaded',%s) returning id",
            (team_id, uploader, path, thread_id),
        ).fetchone()[0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# One object, one document
# ---------------------------------------------------------------------------

def test_a_second_document_cannot_claim_another_teams_object(seeded):
    """🔴 The cross-team import. Team B points a row of its own at team A's
    file and has the privileged worker read it."""
    victim = _path(TEAM_A)
    _document(TEAM_A, A1, victim)

    with pytest.raises(psycopg.errors.UniqueViolation):
        _document(TEAM_B, B1, victim)


def test_a_second_document_cannot_claim_a_restricted_attachment(seeded):
    """The same move inside one team, which a team-prefix check cannot see:
    the path starts with the right team id and belongs to a thread the caller
    is not in."""
    conn = _admin()
    try:
        thread_id = conn.execute(
            "insert into public.threads (team_id, title, visibility, kind,"
            " created_by) values (%s,'Security review','restricted',"
            "'discussion',%s) returning id", (TEAM_A, A1),
        ).fetchone()[0]
        conn.execute(
            "insert into public.thread_participants (thread_id, team_id,"
            " user_id, added_by) values (%s,%s,%s,%s)",
            (thread_id, TEAM_A, A1, A1),
        )
    finally:
        conn.close()
    attachment = _path(TEAM_A, "incident.pdf")
    _document(TEAM_A, A1, attachment, thread_id=thread_id)

    # A2 is in the team and not in the thread.
    with pytest.raises(psycopg.errors.UniqueViolation):
        _document(TEAM_A, A2, attachment)


def test_a_document_cannot_be_repointed_after_it_exists(seeded):
    """🔴 The two-step version: upload something harmless, let the row be
    accepted, then edit `storage_path` to the file you actually want read."""
    mine = _path(TEAM_B)
    theirs = _path(TEAM_A)
    _document(TEAM_A, A1, theirs)
    document_id = _document(TEAM_B, B1, mine)

    conn = _admin()
    try:
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute(
                "update public.documents set storage_path=%s where id=%s",
                (theirs, document_id),
            )
    finally:
        conn.close()


def test_a_member_cannot_repoint_through_the_api_either(seeded):
    """The trigger has to hold for the role members actually use, not only
    for the table owner."""
    document_id = _document(TEAM_A, A1, _path(TEAM_A))

    with pytest.raises(psycopg.errors.RaiseException):
        with as_user(A1, commit=True) as conn:
            conn.execute(
                "update public.documents set storage_path=%s where id=%s",
                (_path(TEAM_A, "other.pdf"), document_id),
            )


# ---------------------------------------------------------------------------
# What must keep working
# ---------------------------------------------------------------------------

def test_recording_the_path_after_the_upload_still_works(seeded):
    """The ordinary flow writes the row first and the path when the upload
    finishes. Null to a path is not a re-point."""
    conn = _admin()
    try:
        document_id = conn.execute(
            "insert into public.documents (team_id, uploader_id, kind,"
            " filename, status) values (%s,%s,'text','notes.md','uploaded')"
            " returning id", (TEAM_A, A1),
        ).fetchone()[0]
        conn.execute(
            "update public.documents set storage_path=%s where id=%s",
            (_path(TEAM_A, "notes.md"), document_id),
        )
    finally:
        conn.close()


def test_two_documents_with_no_file_are_fine(seeded):
    """The unique index is partial. Rows that reference no object are not in
    competition for one."""
    conn = _admin()
    try:
        for _ in range(2):
            conn.execute(
                "insert into public.documents (team_id, uploader_id, kind,"
                " filename, status) values (%s,%s,'text','notes.md','uploaded')",
                (TEAM_A, A1),
            )
    finally:
        conn.close()


def test_an_ordinary_upload_is_untouched(seeded):
    """Different files, different rows, no interference."""
    _document(TEAM_A, A1, _path(TEAM_A, "one.pdf"))
    _document(TEAM_A, A2, _path(TEAM_A, "two.pdf"))
