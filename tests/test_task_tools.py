"""Tests for now(), task_get, and task_propose_update.

The controlling constraint (see supabase/migrations/20260612101500_triggers.sql,
trg_tasks_confirm_guard): only the assignee may move a task out of 'proposed'
or set confirmed_at, and the guard checks auth.uid() -- which is NULL for
comrade_executor. task_propose_update may therefore amend only title,
description, deadline and assignee_id, and shared/consent.py must refuse a
proposal that reaches for status or confirmed_at LOUDLY, not by quietly
dropping the disallowed part -- a human approved a card, and the card must not
lie about what actually happened.

Every isolation test here follows tests/test_read_tools.py's rule: prove the
query CAN see something before proving it cannot see the thing it must not.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import psycopg
import pytest

from agent.tools import fetch_task, now, propose_task_update, task_get, task_propose_update
from shared.config import settings
from shared.consent import (
    ConsentError, edit_and_approve, execute_consent, propose_action, resolve_tier,
)
from tests._seed import A1, A2, B1, B2, TEAM_A, TEAM_B, as_user, count


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def _join_team_b(admin, user_id):
    """Make `user_id` a member of TEAM_B as well (mirrors test_read_tools.py)."""
    admin.execute(
        "insert into public.memberships (team_id, user_id, role, status)"
        " values (%s,%s,'member','active')",
        (TEAM_B, user_id),
    )


def _make_task(admin, team_id=TEAM_A, assignee=A2, title="Original title",
               description="Original description", deadline=None, status="proposed"):
    """Insert a task via admin. Trigger still fires: INSERT must land 'proposed'
    with no confirmed_at, so `status` here is never anything else."""
    return admin.execute(
        "insert into public.tasks (team_id, assignee_id, title, description,"
        " deadline, status, created_by_kind, created_by_id)"
        " values (%s,%s,%s,%s,%s,%s,'user',%s) returning id",
        (team_id, assignee, title, description, deadline, status, A1),
    ).fetchone()[0]


def _task_row(admin, task_id):
    """(title, description, deadline, assignee_id, status, confirmed_at)."""
    return admin.execute(
        "select title, description, deadline, assignee_id, status, confirmed_at"
        " from public.tasks where id=%s",
        (task_id,),
    ).fetchone()


def _approve(admin, consent_id):
    admin.execute(
        "update public.consent_queue set status='approved' where id=%s", (consent_id,)
    )


def _ctx(team_id=TEAM_A, requester_id=A1):
    return SimpleNamespace(state={"team_id": team_id, "requester_id": requester_id})


# ---------------------------------------------------------------------------
# now()
# ---------------------------------------------------------------------------

def test_now_returns_current_utc_time_iso8601():
    before = datetime.now(timezone.utc)
    result = now(tool_context=None)  # touches no state at all
    after = datetime.now(timezone.utc)

    ts = datetime.fromisoformat(result["now"])
    assert ts.tzinfo is not None and ts.utcoffset().total_seconds() == 0
    assert before <= ts <= after


# ---------------------------------------------------------------------------
# task_get / fetch_task
# ---------------------------------------------------------------------------

def test_task_get_returns_the_fields_a_model_needs(seeded, admin):
    tid = _make_task(admin, description="Ship the thing")

    task = fetch_task(TEAM_A, A1, str(tid))

    assert task["title"] == "Original title"
    assert task["description"] == "Ship the thing"
    assert task["status"] == "proposed"
    assert task["assignee"] == "A2"  # display name, not the raw id
    assert task["confirmed_at"] is None
    assert task["created_at"]


def test_task_get_shows_deadline_and_confirmation(seeded, admin):
    tid = _make_task(admin, deadline=datetime(2026, 9, 1, tzinfo=timezone.utc))
    with as_user(A2, commit=True) as conn:  # A2 is the assignee
        conn.execute(
            "update public.tasks set status='confirmed', confirmed_at=now()"
            " where id=%s",
            (tid,),
        )

    task = fetch_task(TEAM_A, A1, str(tid))

    assert task["status"] == "confirmed"
    assert task["confirmed_at"] is not None
    assert task["deadline"].startswith("2026-09-01")


def test_task_get_on_missing_task_is_not_found(seeded):
    assert fetch_task(
        TEAM_A, A1, "00000000-0000-0000-0000-000000000000"
    ) == {"error": "no such task"}


def test_task_get_survives_an_id_the_model_invented(seeded):
    assert fetch_task(TEAM_A, A1, "not-a-uuid") == {"error": "no such task"}


def test_task_get_on_another_teams_task_is_not_found(seeded, admin):
    """Non-vacuous: A1 joins TEAM_B and genuinely CAN read the row there under
    `authenticated`, so what follows tests the explicit team_id filter -- not
    RLS doing the work by accident."""
    _join_team_b(admin, A1)
    b_task = _make_task(admin, team_id=TEAM_B, assignee=B2)

    with as_user(A1) as conn:
        assert count(
            conn, "select count(*) from public.tasks where id=%s", (b_task,)
        ) == 1

    assert fetch_task(TEAM_B, A1, str(b_task))["title"] == "Original title"  # not blind
    assert fetch_task(TEAM_A, A1, str(b_task)) == {"error": "no such task"}


def test_the_task_get_wrapper_reads_ids_from_state(seeded, admin):
    tid = _make_task(admin)
    assert task_get(str(tid), _ctx())["title"] == "Original title"


# ---------------------------------------------------------------------------
# task_propose_update: proposes, does not act
# ---------------------------------------------------------------------------

def test_propose_task_update_writes_pending_without_acting(seeded, admin):
    tid = _make_task(admin)

    result = propose_task_update(TEAM_A, A1, str(tid), deadline="2026-09-10T00:00:00Z")

    assert result["status"] == "pending"
    status, = admin.execute(
        "select status from public.consent_queue where id=%s", (result["consent_id"],)
    ).fetchone()
    assert status == "pending"
    assert _task_row(admin, tid)[2] is None  # deadline untouched


def test_task_update_tier_floor_is_t1():
    assert resolve_tier("task_update", None) == "T1"


# ---------------------------------------------------------------------------
# the partial-update guarantee: a proposal only touches what it mentions
# ---------------------------------------------------------------------------

def test_approving_a_deadline_only_change_leaves_other_fields_alone(seeded, admin):
    tid = _make_task(admin, title="Keep me", description="Keep me too", assignee=A2)

    result = propose_task_update(TEAM_A, A1, str(tid), deadline="2026-09-10T00:00:00Z")
    _approve(admin, result["consent_id"])
    outcome = execute_consent(TEAM_A, result["consent_id"])

    assert outcome["status"] == "executed"
    title, description, deadline, assignee_id, _status, _confirmed = _task_row(admin, tid)
    assert title == "Keep me"
    assert description == "Keep me too"
    assert str(assignee_id) == A2
    assert deadline is not None and deadline.isoformat().startswith("2026-09-10")


def test_omitting_deadline_leaves_it_alone_while_title_changes(seeded, admin):
    tid = _make_task(admin, deadline=datetime(2026, 1, 1, tzinfo=timezone.utc))

    result = propose_task_update(TEAM_A, A1, str(tid), title="New title")
    _approve(admin, result["consent_id"])
    execute_consent(TEAM_A, result["consent_id"])

    title, _description, deadline, _assignee, _status, _confirmed = _task_row(admin, tid)
    assert title == "New title"
    assert deadline is not None  # untouched, not nulled


def test_clearing_the_deadline_explicitly_sets_it_null(seeded, admin):
    tid = _make_task(admin, deadline=datetime(2026, 1, 1, tzinfo=timezone.utc))

    result = propose_task_update(TEAM_A, A1, str(tid), deadline="")
    _approve(admin, result["consent_id"])
    execute_consent(TEAM_A, result["consent_id"])

    assert _task_row(admin, tid)[2] is None


def test_clearing_the_description_explicitly_sets_it_null(seeded, admin):
    tid = _make_task(admin, description="has a description")

    result = propose_task_update(TEAM_A, A1, str(tid), description="")
    _approve(admin, result["consent_id"])
    execute_consent(TEAM_A, result["consent_id"])

    assert _task_row(admin, tid)[1] is None


def test_blank_title_and_assignee_mean_not_mentioned_not_cleared(seeded, admin):
    """title/assignee_id follow team_propose_task's own convention: "" is not
    a value, so it cannot retitle to blank or unassign -- only deadline and
    description are genuinely clearable through this tool."""
    tid = _make_task(admin, title="Keep title", assignee=A2)

    result = propose_task_update(
        TEAM_A, A1, str(tid), title="", assignee_id="",
        deadline="2026-11-01T00:00:00Z",
    )
    _approve(admin, result["consent_id"])
    execute_consent(TEAM_A, result["consent_id"])

    title, _description, deadline, assignee_id, _status, _confirmed = _task_row(admin, tid)
    assert title == "Keep title"
    assert str(assignee_id) == A2
    assert deadline is not None  # the one field actually mentioned did change


def test_proposing_no_changes_at_all_is_rejected_at_execute_time(seeded, admin):
    tid = _make_task(admin)

    result = propose_task_update(TEAM_A, A1, str(tid))  # nothing mentioned
    _approve(admin, result["consent_id"])

    with pytest.raises(ConsentError, match="changes nothing"):
        execute_consent(TEAM_A, result["consent_id"])


# ---------------------------------------------------------------------------
# reassignment through the executor voids confirmation
# ---------------------------------------------------------------------------

def test_reassigning_a_confirmed_task_through_the_executor_voids_confirmation(seeded, admin):
    """test_triggers.py already proves the trigger voids confirmation under
    `as_user`. This proves the EXECUTOR path (comrade_executor, auth.uid()
    NULL) reaches the same trigger and comes out the other side clean --
    which is the whole point: the guard's assignee-only branch must not even
    fire for a same-status, no-confirm update, and the reassignment branch
    must fire regardless of auth.uid() at all."""
    tid = _make_task(admin, assignee=A2)
    with as_user(A2, commit=True) as conn:
        conn.execute(
            "update public.tasks set status='confirmed', confirmed_at=now()"
            " where id=%s",
            (tid,),
        )
    pre = _task_row(admin, tid)
    assert pre[4] == "confirmed" and pre[5] is not None

    result = propose_task_update(TEAM_A, A1, str(tid), assignee_id=A1)
    _approve(admin, result["consent_id"])
    outcome = execute_consent(TEAM_A, result["consent_id"])

    assert outcome["status"] == "executed"
    _title, _description, _deadline, assignee_id, status, confirmed_at = _task_row(admin, tid)
    assert str(assignee_id) == A1
    assert status == "proposed"
    assert confirmed_at is None


def test_reassigning_to_a_non_member_is_rejected(seeded, admin):
    tid = _make_task(admin, assignee=A2)

    result = propose_task_update(TEAM_A, A2, str(tid), assignee_id=B1)  # B1: TEAM_B only
    _approve(admin, result["consent_id"])

    with pytest.raises(ConsentError, match="active team member"):
        execute_consent(TEAM_A, result["consent_id"])


def test_precheck_rejects_a_task_deleted_before_approval(seeded, admin):
    tid = _make_task(admin)
    result = propose_task_update(TEAM_A, A1, str(tid), title="New title")
    admin.execute("delete from public.tasks where id=%s", (tid,))
    _approve(admin, result["consent_id"])

    with pytest.raises(ConsentError, match="no longer exists"):
        execute_consent(TEAM_A, result["consent_id"])


# ---------------------------------------------------------------------------
# THE invariant: a proposal can never move status or set confirmed_at
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_key,bad_value", [
    ("status", "confirmed"),
    ("confirmed_at", "2026-01-01T00:00:00Z"),
])
def test_a_task_update_touching_status_or_confirmed_at_is_rejected_at_proposal(
    seeded, admin, bad_key, bad_value
):
    """T22 moved this check EARLIER as well as keeping it.

    It used to run only in the executor, so the agent could queue a card
    asking to close work, a member could approve it, and only then would it
    fail — somebody approving something that cannot happen. `propose_action`
    refuses it now, which is the same rule it already applied to a tool with
    no executor at all.
    """
    tid = _make_task(admin)
    args = {"task_id": str(tid), "title": "Sneaked in", bad_key: bad_value}

    with pytest.raises(ConsentError):
        propose_action(TEAM_A, A1, "task_update", args)


@pytest.mark.parametrize("bad_key,bad_value", [
    ("status", "confirmed"),
    ("confirmed_at", "2026-01-01T00:00:00Z"),
])
def test_a_task_update_edited_to_touch_status_is_still_rejected(
    seeded, admin, bad_key, bad_value
):
    """The entry point the propose-time check cannot cover.

    task_propose_update's own signature has no status/confirmed_at parameter,
    and proposals carrying one are refused up front — but a HUMAN can still
    edit a pending proposal's args through edit_and_approve. The executor has
    to refuse it too, and visibly: if it quietly dropped the bad key and
    applied the rest, the approved card would show something different from
    what actually happened.
    """
    tid = _make_task(admin)
    result = propose_action(
        TEAM_A, A1, "task_update", {"task_id": str(tid), "title": "Sneaked in"},
    )

    with pytest.raises(ConsentError):
        edit_and_approve(
            TEAM_A, result["consent_id"], A1,
            {"task_id": str(tid), "title": "Sneaked in", bad_key: bad_value},
        )

    # Visible, not a silent no-op: neither field changed, not even the
    # innocuous title that rode along with the disallowed key.
    assert _task_row(admin, tid)[0] == "Original title"


# ---------------------------------------------------------------------------
# the ADK wrapper binds ids from session state, never from model arguments
# ---------------------------------------------------------------------------

def test_the_propose_wrapper_binds_ids_from_state_not_arguments(seeded, admin):
    tid = _make_task(admin)

    result = task_propose_update(str(tid), _ctx(), deadline="2026-10-01T00:00:00Z")

    assert result["status"] == "pending"
    requester_id, team_id = admin.execute(
        "select requesting_member_id, team_id from public.consent_queue where id=%s",
        (result["consent_id"],),
    ).fetchone()
    assert str(requester_id) == A1 and str(team_id) == TEAM_A
