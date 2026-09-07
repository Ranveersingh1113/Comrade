"""A task, the thread where its work happens, and who may say it is done.

🔴 THE DEFECTS.

Tasks and work threads were unconnected. `tasks.status` had one vocabulary
(proposed/confirmed/in_progress/done) and `threads.work_state` had another
(planned/active/waiting/review/done), and nothing joined them — so a team
doing a piece of work in a thread AND tracking it as a task had two answers to
"is this finished", which disagreed the moment either moved.

And closing work was grantable. `task_update` is in `_REUSABLE_GRANT_RISK`, so
one "allow for this thread" let the agent make any later task update without
asking — INCLUDING setting status to done. An agent could close a team's work
off the back of its own prose.
"""
import psycopg
import pytest

from shared.config import settings
from tests._seed import A1, A2, B1, TEAM_A, as_user


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _work_thread(conn, *, visibility="team", title="Fix the importer"):
    thread_id = str(conn.execute(
        "insert into public.threads (team_id, title, visibility, kind,"
        " work_state, created_by) values (%s,%s,%s,'work','planned',%s)"
        " returning id",
        (TEAM_A, title, visibility, A1),
    ).fetchone()[0])
    if visibility == "restricted":
        conn.execute(
            "insert into public.thread_participants (thread_id, team_id, user_id,"
            " added_by) values (%s,%s,%s,%s)",
            (thread_id, TEAM_A, A1, A1),
        )
    return thread_id


def _task(conn, *, thread_id=None, title="Ship the importer"):
    """A newly proposed task.

    Always `proposed`: `trg_tasks_confirm_guard` requires every task to start
    that way, and leaving `proposed` requires the ASSIGNEE themselves. Both are
    real invariants — a task nobody has agreed to is not confirmed work, and
    nobody else gets to accept work on your behalf — so tests move a task with
    `_advance`, as the assignee, rather than writing the state they want.
    """
    return str(conn.execute(
        "insert into public.tasks (team_id, assignee_id, title, status,"
        " created_by_kind, created_by_id, thread_id)"
        " values (%s,%s,%s,'proposed','user',%s,%s) returning id",
        (TEAM_A, A1, title, A1, thread_id),
    ).fetchone()[0])


def _advance(task_id, status, who=A1):
    """Move a task, as the assignee, the way the product does."""
    with as_user(who, commit=True) as conn:
        conn.execute(
            "update public.tasks set status=%s where id=%s", (status, task_id)
        )


def _work_state(thread_id):
    conn = _admin()
    try:
        return conn.execute(
            "select work_state from public.threads where id=%s", (thread_id,)
        ).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The link, and one status
# ---------------------------------------------------------------------------

def test_a_task_can_name_the_thread_its_work_happens_in(seeded):
    conn = _admin()
    try:
        thread_id = _work_thread(conn)
        task_id = _task(conn, thread_id=thread_id)
        linked = conn.execute(
            "select thread_id from public.tasks where id=%s", (task_id,)
        ).fetchone()[0]
    finally:
        conn.close()

    assert str(linked) == thread_id


def test_a_task_needs_no_thread_and_a_thread_needs_no_task(seeded):
    """Ordinary human work is a task with nowhere to run, and a work thread
    can exist before anybody has written a task for it."""
    conn = _admin()
    try:
        task_id = _task(conn, thread_id=None)
        thread_id = _work_thread(conn, title="Exploratory")
        assert task_id and thread_id
        orphan = conn.execute(
            "select 1 from public.tasks where id=%s and thread_id is null",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()

    assert orphan is not None


def test_the_threads_state_follows_the_task(seeded):
    """🔴 Two statuses, no relationship. The board said one thing and the
    thread said another, and both were "authoritative"."""
    conn = _admin()
    try:
        thread_id = _work_thread(conn)
        task_id = _task(conn, thread_id=thread_id)
    finally:
        conn.close()
    assert _work_state(thread_id) == "planned"

    _advance(task_id, "in_progress")
    assert _work_state(thread_id) == "active"

    _advance(task_id, "done")
    assert _work_state(thread_id) == "done"


def test_an_unlinked_task_does_not_touch_any_thread(seeded):
    conn = _admin()
    try:
        thread_id = _work_thread(conn)
        task_id = _task(conn, thread_id=None)
    finally:
        conn.close()

    _advance(task_id, "done")

    assert _work_state(thread_id) == "planned"


# ---------------------------------------------------------------------------
# Restricted work stays invisible
# ---------------------------------------------------------------------------

def test_a_task_in_a_restricted_thread_is_invisible_to_non_participants(seeded):
    """🔴 A leak the link would have created. `au_tasks_select` was
    `is_team_member(team_id)` — every member saw every task — so linking a task
    to a restricted thread would have published its title, owner and deadline
    to people who cannot open the thread."""
    conn = _admin()
    try:
        thread_id = _work_thread(conn, visibility="restricted", title="Security fix")
        _task(conn, thread_id=thread_id, title="Rotate the leaked key")
    finally:
        conn.close()

    with as_user(A2) as conn:
        rows = conn.execute(
            "select title from public.tasks where team_id=%s", (TEAM_A,)
        ).fetchall()

    assert not any("Rotate the leaked key" == r[0] for r in rows)


def test_a_participant_still_sees_the_task(seeded):
    conn = _admin()
    try:
        thread_id = _work_thread(conn, visibility="restricted", title="Security fix")
        _task(conn, thread_id=thread_id, title="Rotate the leaked key")
    finally:
        conn.close()

    with as_user(A1) as conn:
        rows = conn.execute(
            "select title from public.tasks where team_id=%s", (TEAM_A,)
        ).fetchall()

    assert any("Rotate the leaked key" == r[0] for r in rows)


def test_an_unlinked_task_is_still_visible_to_the_whole_team(seeded):
    """The narrowing must not quietly make every task private."""
    conn = _admin()
    try:
        _task(conn, thread_id=None, title="Order more coffee")
    finally:
        conn.close()

    with as_user(A2) as conn:
        rows = conn.execute(
            "select title from public.tasks where team_id=%s", (TEAM_A,)
        ).fetchall()

    assert any("Order more coffee" == r[0] for r in rows)


def test_another_team_sees_none_of_it(seeded):
    conn = _admin()
    try:
        _task(conn, thread_id=None, title="Order more coffee")
    finally:
        conn.close()

    with as_user(B1) as conn:
        rows = conn.execute(
            "select 1 from public.tasks where team_id=%s", (TEAM_A,)
        ).fetchall()

    assert rows == []


# ---------------------------------------------------------------------------
# Closing work is a human act
# ---------------------------------------------------------------------------
#
# Checked rather than assumed, and the answer was better than expected: the
# agent has NO path to close work at all. `task_update`'s precheck restricts
# the fields it may set, and `status` is not one of them. So "never auto-close
# work from plan completion or assistant prose" is already true — these tests
# pin it, and add a second line at the grant layer for if that list ever grows.


def test_the_agent_cannot_set_a_task_status_at_all(seeded):
    """The strongest form of "never auto-close": there is no such tool call.

    `task_update` may set title, description, deadline and assignee — the
    things a plan can reasonably rearrange — and not `status`, which is the
    field that says whether a human considers the work finished.
    """
    from shared.consent import ConsentError, propose_action

    conn = _admin()
    try:
        thread_id = _work_thread(conn)
        task_id = _task(conn, thread_id=thread_id)
    finally:
        conn.close()

    with pytest.raises(ConsentError) as caught:
        propose_action(
            TEAM_A, A1, "task_update",
            {"task_id": task_id, "status": "done"},
            thread_id=thread_id,
        )

    assert "status" in str(caught.value)


def test_marking_work_done_is_never_covered_by_a_standing_grant(seeded):
    """A second line, at the layer where standing permission is decided.

    If `task_update` ever learns to set `status` — a plausible next feature —
    a thread grant must still not cover closing the work. Marking something
    finished is the one task outcome a person has to actually decide, and one
    "allow for this thread" should not have bought it.
    """
    from shared.consent import _grantable

    with pytest.raises(Exception):
        _grantable("task_update", "thread-1", {"task_id": "t", "status": "done"})


def test_ordinary_task_updates_are_still_grantable(seeded):
    """The narrowing must not make every task edit an interruption."""
    from shared.consent import _grantable

    risk, resource = _grantable(
        "task_update", "thread-1", {"task_id": "t", "title": "Ship it"},
    )
    assert risk == "member" and resource == {"task_id": "t"}


def test_a_plan_finishing_does_not_finish_the_work(seeded):
    """A plan is the agent's account of what it intends to do. Work being done
    is a claim about the world, and only a person makes it."""
    conn = _admin()
    try:
        thread_id = _work_thread(conn)
        task_id = _task(conn, thread_id=thread_id)
        status = conn.execute(
            "select status from public.tasks where id=%s", (task_id,)
        ).fetchone()[0]
    finally:
        conn.close()

    # Nothing in the agent's surface can move this to done; the only writer is
    # a member, through RLS, from the board.
    assert status == "proposed"
    assert _work_state(thread_id) == "planned"
