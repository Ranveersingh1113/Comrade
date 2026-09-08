"""Who can read the bytes, as opposed to the row about them.

🔴 THE DEFECT (fix.md F18). `st_documents_select` authorized by the FIRST PATH
SEGMENT — `is_team_member(<team id from the path>)`. T24 gave documents a
thread and a purpose, so an attachment dropped in a restricted conversation is
denied to a non-participant by `au_documents_select`, and the FILE was readable
by any member of the team straight out of Storage: a signed URL, or a direct
object read, without touching the documents API or the frontend at all.

Scoping the metadata and leaving the bytes on a team-wide rule is not a
boundary, it is a detour sign.

Tested against `storage.objects` itself rather than through the Storage HTTP
API: the policy is the thing that was wrong, and it is the thing that has to
be right for a signed-URL request, a list, and a direct read alike.
"""
import uuid

import psycopg
import pytest

from shared.config import settings
from tests._seed import A1, A2, B1, TEAM_A, as_user


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _object(path: str) -> None:
    """A row in storage.objects, as an upload would leave behind."""
    conn = _admin()
    try:
        conn.execute(
            "insert into storage.objects (bucket_id, name, owner_id)"
            " values ('documents', %s, %s)"
            " on conflict do nothing", (path, A1),
        )
    finally:
        conn.close()


def _document(path: str, *, thread_id=None, deleted: bool = False) -> None:
    conn = _admin()
    try:
        conn.execute(
            "insert into public.documents (team_id, uploader_id, kind,"
            " filename, storage_path, status, thread_id, deleted_at) values"
            " (%s,%s,'text','incident.md',%s,'ready',%s,"
            "  case when %s then now() else null end)",
            (TEAM_A, A1, path, thread_id, deleted),
        )
    finally:
        conn.close()


def _restricted_thread() -> str:
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
    return str(thread_id)


def _can_read(user_id: str, path: str) -> bool:
    with as_user(user_id) as conn:
        rows = conn.execute(
            "select 1 from storage.objects where bucket_id='documents'"
            "  and name=%s", (path,),
        ).fetchall()
    return bool(rows)


@pytest.fixture
def attachment(seeded):
    """A file attached to a thread only A1 may open."""
    path = f"{TEAM_A}/{uuid.uuid4()}-incident.md"
    thread_id = _restricted_thread()
    _object(path)
    _document(path, thread_id=thread_id)
    return path, thread_id


# ---------------------------------------------------------------------------

def test_a_teammate_cannot_read_a_restricted_attachment(attachment):
    """🔴 They could. The path starts with their own team id, and that was
    the whole check."""
    path, _ = attachment

    assert not _can_read(A2, path)


def test_the_participant_can(attachment):
    """A boundary that denies everybody is not a boundary, it is an outage."""
    path, _ = attachment

    assert _can_read(A1, path)


def test_another_team_cannot(attachment):
    path, _ = attachment

    assert not _can_read(B1, path)


def test_a_team_wide_document_is_readable_by_the_team(seeded):
    """A file uploaded on the team screen has no thread, and every member
    should still be able to open it."""
    path = f"{TEAM_A}/{uuid.uuid4()}-handbook.md"
    _object(path)
    _document(path)

    assert _can_read(A2, path)


def test_adding_someone_to_the_thread_lets_them_read_it(attachment):
    """Access is evaluated on read, so a grant reaches the bytes."""
    path, thread_id = attachment
    assert not _can_read(A2, path)

    conn = _admin()
    try:
        conn.execute(
            "insert into public.thread_participants (thread_id, team_id,"
            " user_id, added_by) values (%s,%s,%s,%s)",
            (thread_id, TEAM_A, A2, A1),
        )
    finally:
        conn.close()

    assert _can_read(A2, path)


def test_a_deleted_documents_file_is_no_longer_readable(seeded):
    """Deletion here is soft by design. "Soft" should not mean the file
    stayed readable to everyone who could reach it before."""
    path = f"{TEAM_A}/{uuid.uuid4()}-old.md"
    _object(path)
    _document(path, deleted=True)

    assert not _can_read(A1, path)


def test_an_orphan_object_is_readable_by_nobody(seeded):
    """Uploaded, and the row never arrived. The frontend uploads first and
    inserts second, so this window exists — and the right way for it to fail
    is closed."""
    path = f"{TEAM_A}/{uuid.uuid4()}-orphan.md"
    _object(path)

    assert not _can_read(A1, path)
    assert not _can_read(A2, path)


def test_uploading_is_still_possible_before_the_row_exists(seeded):
    """The other half of that window: INSERT cannot consult a document that
    does not exist yet, so it stays on the team rule."""
    path = f"{TEAM_A}/{uuid.uuid4()}-new.md"

    with as_user(A1, commit=True) as conn:
        conn.execute(
            "insert into storage.objects (bucket_id, name, owner_id)"
            " values ('documents', %s, %s)", (path, A1),
        )
