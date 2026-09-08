"""Reading an object that belongs to somebody else.

🔴 THE DEFECT (fix.md F20, REOPENED by the 2026-09-09 review).

`documents.storage_path` is written by MEMBERS — the browser uploads to Storage
and inserts the row under RLS — and `shared/storage.py:download_document`
fetches whatever path it is handed using the Supabase SERVICE SECRET, which is
not subject to RLS at all. Knowing a path is a prerequisite, not authority.

The first repair added two structural guards, and they are both real:

  * UNIQUE on `storage_path`: a second document row cannot point at an object
    that already belongs to one.
  * IMMUTABLE: a row cannot be re-pointed after insert.

Between them they close aliasing to an object that HAS a document row. They do
nothing about an object that does not:

  * the upload-before-metadata interval — the object lands, the insert fails,
    and the object sits there owned by nobody's row;
  * an object whose document row was hard-deleted.

And the migration that added them says, in a comment I wrote:

    `shared/storage.py` additionally refuses a path outside the reading team's
    own prefix

`download_document(storage_path)` takes a path and nothing else. There is no
team argument and no check. The comment describes code that does not exist —
the same failure as A04's docstring, in the file that claims the guarantee.

A prefix check would not be enough anyway, and that is the point of the second
test below: a restricted same-team attachment is under the reader's OWN team
prefix, so the check the comment promised would have passed it.

WHAT AUTHENTICITY MEANS HERE. Every upload reaches this bucket through the
member's own browser session, so Storage records `owner_id` = that member, and
the row the member then inserts carries `uploader_id` = the same member. A row
claiming an object it did not upload is the whole attack, in one comparison.
"""
import uuid

import psycopg
import pytest

from shared.config import settings
from shared.storage import ObjectNotOwned, download_document
from tests._seed import A1, A2, B1, TEAM_A, TEAM_B


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _object(path: str, owner: str) -> None:
    """An object in Storage, as an upload by `owner` would leave it."""
    conn = _admin()
    try:
        conn.execute(
            "insert into storage.objects (bucket_id, name, owner_id)"
            " values ('documents', %s, %s) on conflict do nothing",
            (path, owner),
        )
    finally:
        conn.close()


def _document(*, team: str, uploader: str, path: str, thread_id=None) -> str:
    conn = _admin()
    try:
        return str(conn.execute(
            "insert into public.documents (team_id, uploader_id, kind,"
            " filename, storage_path, status, thread_id)"
            " values (%s,%s,'text','notes.md',%s,'uploaded',%s) returning id",
            (team, uploader, path, thread_id),
        ).fetchone()[0])
    finally:
        conn.close()


def _restricted_thread(owner: str) -> str:
    """A restricted thread `owner` is in and A1 is not."""
    conn = _admin()
    try:
        thread_id = conn.execute(
            "insert into public.threads (team_id, title, visibility, kind,"
            " created_by) values (%s,'Security review','restricted',"
            "'discussion',%s) returning id", (TEAM_A, owner),
        ).fetchone()[0]
        conn.execute(
            "insert into public.thread_participants (thread_id, team_id,"
            " user_id, added_by) values (%s,%s,%s,%s)",
            (thread_id, TEAM_A, owner, owner),
        )
        return str(thread_id)
    finally:
        conn.close()


@pytest.fixture
def clean(seeded):
    conn = _admin()
    try:
        conn.execute("delete from public.documents")
        # storage.objects is NOT cleaned: the stack installs a protect_delete
        # trigger that refuses direct deletes ("Use the Storage API instead").
        # Every object below is named with a fresh uuid, so they do not
        # collide across runs.
    finally:
        conn.close()
    return None


# ---------------------------------------------------------------------------

def test_a_foreign_object_with_no_document_row_cannot_be_claimed(clean):
    """🔴 The finding's own case, in the window the UNIQUE index cannot see.

    Team B uploads; their metadata insert never lands. A1 knows the path and
    files a document row in TEAM_A pointing at it. Nothing today refuses that:
    no other row owns the object, so UNIQUE passes; the row is inserted rather
    than updated, so the immutability trigger never fires; and the privileged
    read has no opinion about paths at all.
    """
    path = f"{TEAM_B}/{uuid.uuid4()}-secret.md"
    _object(path, B1)                       # uploaded by team B
    document_id = _document(team=TEAM_A, uploader=A1, path=path)

    with pytest.raises(ObjectNotOwned):
        download_document(path, team_id=TEAM_A, document_id=document_id)


def test_a_restricted_same_team_object_cannot_be_claimed_either(clean):
    """The case a team-prefix check would wave straight through.

    The object is under TEAM_A's own prefix — the reader's own team — and it
    belongs to a restricted thread A1 is not in. This is why the comment's
    promised prefix check would not have been a fix even if it had existed.
    """
    thread_id = _restricted_thread(A2)
    path = f"{TEAM_A}/{uuid.uuid4()}-private.md"
    _object(path, A2)                       # uploaded by A2, into their thread
    document_id = _document(team=TEAM_A, uploader=A1, path=path,
                            thread_id=thread_id)

    with pytest.raises(ObjectNotOwned):
        download_document(path, team_id=TEAM_A, document_id=document_id)


def test_the_owner_of_an_object_can_still_read_their_own(clean):
    """The check has to leave the ordinary path alone: upload, insert, parse."""
    path = f"{TEAM_A}/{uuid.uuid4()}-notes.md"
    _object(path, A1)
    document_id = _document(team=TEAM_A, uploader=A1, path=path)

    # No ObjectNotOwned. The download itself needs Storage running, so the
    # authenticity gate is what is asserted here; the byte path has its own
    # tests.
    from shared import storage

    storage.assert_object_is_authentic(path, team_id=TEAM_A,
                                       document_id=document_id)


def test_a_path_outside_the_reading_teams_prefix_is_refused(clean):
    """Cheap, and it fails closed before anything else is consulted."""
    path = f"{TEAM_B}/{uuid.uuid4()}-x.md"
    _object(path, A1)                       # even owned by the caller
    document_id = _document(team=TEAM_A, uploader=A1, path=path)

    with pytest.raises(ObjectNotOwned):
        download_document(path, team_id=TEAM_A, document_id=document_id)


@pytest.mark.parametrize("alias", [
    "{team}/../{other}/secret.md",
    "{team}//..//{other}/secret.md",
    "/{team}/../{other}/secret.md",
    "{team}/./../{other}/secret.md",
])
def test_a_noncanonical_path_is_refused_rather_than_normalised(clean, alias):
    """"Reject noncanonical path aliases." A path that traverses out of the
    team's prefix must not be repaired into something acceptable — the storage
    API may resolve it differently from whatever we would decide it means, and
    two answers to "where does this point" is the whole bug."""
    path = alias.format(team=TEAM_A, other=TEAM_B)
    document_id = _document(team=TEAM_A, uploader=A1, path=path)

    with pytest.raises(ObjectNotOwned):
        download_document(path, team_id=TEAM_A, document_id=document_id)


def test_a_traversal_the_database_would_accept_is_still_refused(clean):
    """The case that makes the canonical check load-bearing rather than tidy.

    `storage.objects.name` is text, so an object can literally be NAMED with a
    traversal in it. Then every database check passes — the row records that
    exact string, its first segment is the right team, and the owner matches —
    while the string handed to the Storage HTTP API may well resolve somewhere
    else entirely. That is the "two answers to where does this point" case, and
    only refusing the shape catches it.
    """
    path = f"{TEAM_A}/../{TEAM_A}/legit.md"
    _object(path, A1)
    document_id = _document(team=TEAM_A, uploader=A1, path=path)

    # The database is satisfied; this is what the shape check is for.
    conn = _admin()
    try:
        assert conn.execute(
            "select public.document_object_is_authentic(%s,%s)",
            (document_id, path),
        ).fetchone()[0] is True
    finally:
        conn.close()

    with pytest.raises(ObjectNotOwned):
        download_document(path, team_id=TEAM_A, document_id=document_id)


def test_an_object_that_does_not_exist_is_refused_not_fetched(clean):
    """The upload-before-metadata interval from the other side: a row whose
    object never arrived must not send a privileged request for it."""
    path = f"{TEAM_A}/{uuid.uuid4()}-never-uploaded.md"
    document_id = _document(team=TEAM_A, uploader=A1, path=path)

    with pytest.raises(ObjectNotOwned):
        download_document(path, team_id=TEAM_A, document_id=document_id)


def test_a_path_that_is_not_the_documents_own_is_refused(clean):
    """The read is for ONE document. Handing it another document's path — even
    a legitimate one in the same team — is not that document's content."""
    mine = f"{TEAM_A}/{uuid.uuid4()}-mine.md"
    theirs = f"{TEAM_A}/{uuid.uuid4()}-theirs.md"
    _object(mine, A1)
    _object(theirs, A2)
    document_id = _document(team=TEAM_A, uploader=A1, path=mine)
    _document(team=TEAM_A, uploader=A2, path=theirs)

    with pytest.raises(ObjectNotOwned):
        download_document(theirs, team_id=TEAM_A, document_id=document_id)
