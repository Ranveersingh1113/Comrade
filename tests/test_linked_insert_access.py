"""Writing into a thread you cannot open.

🔴 THE DEFECT (fix.md F29, plus the same hole one table over). T22 narrowed
`tasks` SELECT and UPDATE to `can_access_thread(...)` and left INSERT checking
only team membership. A same-team NON-PARTICIPANT who knows a restricted
thread's id could insert a task pointing at it — and `trg_task_thread_state`,
which is SECURITY DEFINER, then changes that thread's work state. They cannot
read the row back, because SELECT is scoped. The side effect on somebody else's
private thread lands anyway.

`documents` had it too, and worse. Its check was `is_team_member(team_id) and
uploader_id = auth.uid()` with nothing about the thread, so a non-participant
could ATTACH A FILE to a restricted conversation. T24 makes an attachment
`turn_context` — shown to the model for that thread's work — which turns a
state-manipulation bug into a way to put chosen text in front of the agent
inside a thread you are not in.
"""
import uuid

import psycopg
import pytest

from shared.config import settings
from tests._seed import A1, A2, TEAM_A, as_user


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


@pytest.fixture
def restricted(seeded):
    """A work thread only A1 may open. A2 is in the team and not in it."""
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


def _thread_state(thread_id: str) -> str | None:
    conn = _admin()
    try:
        row = conn.execute(
            "select work_state from public.threads where id=%s", (thread_id,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


# ---------------------------------------------------------------------------

def test_a_nonparticipant_cannot_link_a_task_to_a_restricted_thread(restricted):
    """🔴 They could, and the SECURITY DEFINER trigger then moved that
    thread's work state on their behalf."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with as_user(A2, commit=True) as conn:
            conn.execute(
                "insert into public.tasks (team_id, assignee_id, title,"
                " status, created_by_kind, created_by_id, thread_id) values"
                " (%s,%s,'Quietly reassign this','proposed','user',%s,%s)",
                (TEAM_A, A2, A2, restricted),
            )


def test_the_restricted_threads_state_is_untouched(restricted):
    """The side effect is the point: the row they cannot read still changed
    something they cannot see."""
    before = _thread_state(restricted)

    try:
        with as_user(A2, commit=True) as conn:
            conn.execute(
                "insert into public.tasks (team_id, assignee_id, title,"
                " status, created_by_kind, created_by_id, thread_id) values"
                " (%s,%s,'Quietly reassign this','proposed','user',%s,%s)",
                (TEAM_A, A2, A2, restricted),
            )
    except psycopg.Error:
        pass

    assert _thread_state(restricted) == before


def test_a_participant_can_still_link_a_task(restricted):
    """A rule that stops the work is not a fix."""
    with as_user(A1, commit=True) as conn:
        conn.execute(
            "insert into public.tasks (team_id, assignee_id, title, status,"
            " created_by_kind, created_by_id, thread_id) values"
            " (%s,%s,'Rotate the key','proposed','user',%s,%s)",
            (TEAM_A, A1, A1, restricted),
        )


def test_an_unlinked_task_is_still_anyones_to_create(seeded):
    """A task with no thread belongs to the team, and every member may make
    one — narrowing that would be a different product."""
    with as_user(A2, commit=True) as conn:
        conn.execute(
            "insert into public.tasks (team_id, assignee_id, title, status,"
            " created_by_kind, created_by_id) values"
            " (%s,%s,'Ship the release','proposed','user',%s)",
            (TEAM_A, A2, A2),
        )


# ---------------------------------------------------------------------------
# The same rule, one table over
# ---------------------------------------------------------------------------

def test_a_nonparticipant_cannot_attach_a_document_to_a_restricted_thread(
    restricted,
):
    """🔴 The sharper version. An attachment is `turn_context` — shown to the
    model for that thread's work — so this is a way to put chosen text in
    front of the agent inside a conversation you are not part of."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with as_user(A2, commit=True) as conn:
            conn.execute(
                "insert into public.documents (team_id, uploader_id, kind,"
                " filename, storage_path, status, thread_id) values"
                " (%s,%s,'text','instructions.md',%s,'uploaded',%s)",
                (TEAM_A, A2, f"{TEAM_A}/{uuid.uuid4()}-instructions.md",
                 restricted),
            )


def test_a_participant_can_still_attach_one(restricted):
    with as_user(A1, commit=True) as conn:
        conn.execute(
            "insert into public.documents (team_id, uploader_id, kind,"
            " filename, storage_path, status, thread_id) values"
            " (%s,%s,'text','notes.md',%s,'uploaded',%s)",
            (TEAM_A, A1, f"{TEAM_A}/{uuid.uuid4()}-notes.md", restricted),
        )


def test_a_team_upload_with_no_thread_still_works(seeded):
    with as_user(A2, commit=True) as conn:
        conn.execute(
            "insert into public.documents (team_id, uploader_id, kind,"
            " filename, storage_path, status) values"
            " (%s,%s,'text','handbook.md',%s,'uploaded')",
            (TEAM_A, A2, f"{TEAM_A}/{uuid.uuid4()}-handbook.md"),
        )
