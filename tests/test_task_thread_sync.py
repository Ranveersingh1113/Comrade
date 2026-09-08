"""The board and the thread giving two answers to "is this done".

🔴 THE DEFECT (fix.md F28). `trg_tasks_thread_state` was declared
`AFTER INSERT OR UPDATE OF status, thread_id`, and `UPDATE OF` fires on the
columns the STATEMENT names — not on what the row ends up being.

Reassignment updates `assignee_id`. The BEFORE guard then resets `status` to
'proposed', because a task handed to somebody else is not one they have
confirmed. The statement never mentioned `status`, so the sync did not run: the
task became proposed and its thread stayed 'done'.

Which is precisely the divergence T22 was built to close. A trigger that
listens for a statement's shape rather than a row's change will always miss
the changes something else made.
"""
import psycopg
import pytest

from shared.config import settings
from tests._seed import A1, A2, TEAM_A, as_user


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


@pytest.fixture
def linked(seeded):
    """A work thread with a task on it, both finished."""
    conn = _admin()
    try:
        thread_id = conn.execute(
            "insert into public.threads (team_id, title, visibility, kind,"
            " created_by, work_state) values (%s,'Ship the importer','team',"
            "'work',%s,'planned') returning id", (TEAM_A, A1),
        ).fetchone()[0]
        task_id = conn.execute(
            "insert into public.tasks (team_id, assignee_id, title, status,"
            " created_by_kind, created_by_id, thread_id) values"
            " (%s,%s,'Write the importer','proposed','user',%s,%s)"
            " returning id", (TEAM_A, A1, A1, thread_id),
        ).fetchone()[0]
    finally:
        conn.close()
    # Through the states a real task walks, so the thread ends up 'done' — as
    # the ASSIGNEE, because `trg_tasks_confirm_guard` refuses anybody else and
    # the admin connection has no `auth.uid()` at all.
    for status in ("confirmed", "in_progress", "done"):
        with as_user(A1, commit=True) as conn:
            conn.execute(
                "update public.tasks set status=%s where id=%s",
                (status, str(task_id)),
            )
    return str(task_id), str(thread_id)


def _state(thread_id: str) -> str:
    conn = _admin()
    try:
        return conn.execute(
            "select work_state from public.threads where id=%s", (thread_id,),
        ).fetchone()[0]
    finally:
        conn.close()


def _status(task_id: str) -> str:
    conn = _admin()
    try:
        return conn.execute(
            "select status from public.tasks where id=%s", (task_id,),
        ).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------

def test_the_fixture_leaves_both_finished(linked):
    """Without this the test below could pass on a thread that was never
    done in the first place."""
    task_id, thread_id = linked

    assert _status(task_id) == "done"
    assert _state(thread_id) == "done"


def test_reassignment_moves_the_thread_back_too(linked):
    """🔴 It did not. The task became proposed and the thread stayed done —
    two answers to the same question, which is the thing T22 closed and this
    quietly reopened."""
    task_id, thread_id = linked

    conn = _admin()
    try:
        conn.execute(
            "update public.tasks set assignee_id=%s where id=%s",
            (A2, task_id),
        )
    finally:
        conn.close()

    assert _status(task_id) == "proposed", (
        "the confirmation guard did not reset the task"
    )
    assert _state(thread_id) == "planned", (
        "the task went back to proposed and its thread stayed done"
    )


def test_both_change_in_the_same_transaction(linked):
    """A member reading the board mid-flight must never see one without the
    other."""
    task_id, thread_id = linked

    conn = psycopg.connect(settings.comrade_db_url_admin)
    try:
        conn.execute(
            "update public.tasks set assignee_id=%s where id=%s",
            (A2, task_id),
        )
        # Same transaction, not yet committed.
        status = conn.execute(
            "select status from public.tasks where id=%s", (task_id,),
        ).fetchone()[0]
        state = conn.execute(
            "select work_state from public.threads where id=%s", (thread_id,),
        ).fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    assert (status, state) == ("proposed", "planned")


def test_an_edit_that_changes_neither_leaves_the_thread_alone(linked):
    """The trigger now sees every update, so it has to decide from the row.
    Retitling a task is not a change of state."""
    task_id, thread_id = linked
    conn = _admin()
    try:
        before = conn.execute(
            "select updated_at from public.threads where id=%s", (thread_id,),
        ).fetchone()[0]
        conn.execute(
            "update public.tasks set title='Write the importer, properly'"
            " where id=%s", (task_id,),
        )
        after = conn.execute(
            "select updated_at from public.threads where id=%s", (thread_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert after == before, "a title edit wrote to the thread"


def test_the_ordinary_status_walk_still_syncs(linked):
    """The path that already worked has to keep working."""
    task_id, thread_id = linked

    conn = _admin()
    try:
        conn.execute("update public.tasks set status='in_progress'"
                     " where id=%s", (task_id,))
    finally:
        conn.close()

    assert _state(thread_id) == "active"
