"""What the audit log lets a teammate read that the table itself would not.

🔴 THE DEFECT (fix.md F19). `trg_audit` copies the ENTIRE row into
`change_log.before`/`after` with `to_jsonb`, and `change_log`'s select policy
was `is_team_member(team_id)` — team-wide, with no thread scoping at all.

T22 gave tasks a thread and T24 gave documents one, so a task in a restricted
thread and a file attached to a private conversation are both denied to a
non-participant by their own policies. Both were readable in full through the
audit table by any member of the team: the filename, the storage path, the task
title and description, and `documents.parsed_text`, which is the document's
entire contents.

A second table with a copy of the row and different access rules is not an
audit trail, it is a way around the policy.
"""
import psycopg
import pytest

from shared.config import settings
from tests._seed import A1, A2, B1, TEAM_A, as_user

SECRET_TITLE = "Rotate the production signing key"
SECRET_BODY = "the staging password is hunter2"


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


@pytest.fixture
def restricted(seeded):
    """A thread only A1 may open, and a task and document inside it."""
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
        conn.execute(
            "insert into public.tasks (team_id, assignee_id, title,"
            " description, status, created_by_kind, created_by_id, thread_id)"
            " values (%s,%s,%s,%s,'proposed','user',%s,%s)",
            (TEAM_A, A1, SECRET_TITLE, SECRET_BODY, A1, thread_id),
        )
        conn.execute(
            "insert into public.documents (team_id, uploader_id, kind,"
            " filename, storage_path, status, thread_id, parsed_text)"
            " values (%s,%s,'text','incident.md',%s,'ready',%s,%s)",
            (TEAM_A, A1, f"{TEAM_A}/incident.md", thread_id, SECRET_BODY),
        )
    finally:
        conn.close()
    return str(thread_id)


def _audit_text(user_id: str) -> str:
    """Everything this member can read out of the audit table, as one string."""
    with as_user(user_id) as conn:
        rows = conn.execute(
            "select coalesce(before::text,'') || coalesce(after::text,'')"
            "  from public.change_log where team_id=%s", (TEAM_A,),
        ).fetchall()
    return "\n".join(row[0] for row in rows)


# ---------------------------------------------------------------------------

def test_a_teammate_cannot_read_a_restricted_task_through_the_audit(restricted):
    """🔴 They could, in full. The task's own policy denied them the row and
    the audit table handed over a copy of it."""
    assert SECRET_TITLE not in _audit_text(A2)
    assert SECRET_BODY not in _audit_text(A2)


def test_a_participant_still_sees_their_own_history(restricted):
    """The other half. An audit nobody can read is not a fix."""
    assert SECRET_TITLE in _audit_text(A1)


def test_another_team_sees_none_of_it(restricted):
    assert _audit_text(B1) == ""


def test_a_documents_text_is_never_snapshotted_at_all(restricted):
    """Even for someone who MAY see the row. An audit says what happened to a
    document — uploaded, promoted, failed, deleted — not what was in it, and
    republishing the contents into a table with its own access rules is how
    one boundary becomes two."""
    conn = _admin()
    try:
        everything = conn.execute(
            "select coalesce(before::text,'') || coalesce(after::text,'')"
            "  from public.change_log where table_name='documents'"
            "   and team_id=%s", (TEAM_A,),
        ).fetchall()
    finally:
        conn.close()

    combined = "\n".join(row[0] for row in everything)
    assert "parsed_text" not in combined, combined[:400]


def test_team_wide_history_is_still_visible_to_the_team(seeded):
    """A task with no thread belongs to the team, and its history should not
    be narrowed by a fix aimed at restricted ones."""
    conn = _admin()
    try:
        conn.execute(
            "insert into public.tasks (team_id, assignee_id, title, status,"
            " created_by_kind, created_by_id) values"
            " (%s,%s,'Ship the release','proposed','user',%s)",
            (TEAM_A, A1, A1),
        )
    finally:
        conn.close()

    assert "Ship the release" in _audit_text(A2)


def test_removing_a_participant_closes_their_view_of_the_history(restricted):
    """Access is evaluated on read, so revocation reaches what was already
    written."""
    conn = _admin()
    try:
        conn.execute(
            "insert into public.thread_participants (thread_id, team_id,"
            " user_id, added_by) values (%s,%s,%s,%s)",
            (restricted, TEAM_A, A2, A1),
        )
        assert SECRET_TITLE in _audit_text(A2)
        conn.execute(
            "delete from public.thread_participants where thread_id=%s"
            "  and user_id=%s", (restricted, A2),
        )
    finally:
        conn.close()

    assert SECRET_TITLE not in _audit_text(A2)


def test_the_history_survives_the_row_being_deleted(restricted):
    """Deleting the task must not delete the evidence that it existed — the
    audit is the thing that outlives the row."""
    conn = _admin()
    try:
        conn.execute("delete from public.tasks where team_id=%s", (TEAM_A,))
    finally:
        conn.close()

    assert SECRET_TITLE in _audit_text(A1), (
        "the audit lost the record when the source row went"
    )
