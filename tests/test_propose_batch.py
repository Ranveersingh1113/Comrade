"""propose_batch: several proposals sharing one batch_id, each independently
resolvable.

Task 6 brief, non-negotiable design rule: a batch is a DISPLAY grouping, not
an all-or-nothing gate. Every item stays individually approvable and
individually rejectable -- approving/rejecting one must never touch its
siblings. test_approving_one_and_rejecting_another_leaves_the_third_pending_
and_approvable is THE test that proves it.

Partial-failure choice: best-effort, not atomic (see task-6-report.md for the
reasoning). An item whose tool_name has no executor fails on its own; the
rest of the batch still queues and still shares the batch_id.
"""
from types import SimpleNamespace

import psycopg
import pytest

from agent.tools import team_propose_batch
from shared.config import settings
from shared.consent import approve_consent, propose_action, propose_batch, reject_consent
from tests._seed import A1, A2, TEAM_A, TEAM_B


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _ctx(team_id=TEAM_A, requester_id=A1):
    return SimpleNamespace(state={"team_id": team_id, "requester_id": requester_id})


def _task_create(title, assignee=A2):
    return {
        "tool_name": "task_create",
        "args": {"assignee_id": assignee, "title": title, "description": None, "deadline": None},
    }


def _row(conn, consent_id):
    """(batch_id, status) for one consent row."""
    batch_id, status = conn.execute(
        "select batch_id, status from public.consent_queue where id=%s", (consent_id,)
    ).fetchone()
    return (str(batch_id) if batch_id else None), status


# ---------------------------------------------------------------------------
# Step 1: proposing three actions in one batch writes three rows sharing one
# batch_id, each independently approvable.
# ---------------------------------------------------------------------------

def test_propose_batch_writes_rows_sharing_one_batch_id(seeded):
    result = propose_batch(TEAM_A, A1, [
        _task_create("Slide deck"),
        _task_create("Book the room"),
        _task_create("Send invites"),
    ])

    assert result["batch_id"]
    assert len(result["items"]) == 3
    consent_ids = [item["consent_id"] for item in result["items"]]
    assert len(set(consent_ids)) == 3  # three distinct rows, not a dedup collapse

    conn = _admin()
    try:
        rows = [_row(conn, cid) for cid in consent_ids]
    finally:
        conn.close()

    assert all(batch_id == result["batch_id"] for batch_id, _status in rows)
    assert all(status == "pending" for _batch_id, status in rows)


def test_a_lone_propose_action_call_still_writes_a_null_batch_id(seeded):
    """Gate 4: existing behaviour is unaffected by the new optional parameter."""
    cid = propose_action(
        TEAM_A, A1, "task_create",
        {"assignee_id": A2, "title": "solo", "description": None, "deadline": None},
    )["consent_id"]

    conn = _admin()
    try:
        batch_id, _status = _row(conn, cid)
    finally:
        conn.close()
    assert batch_id is None


def test_batch_id_appears_on_no_other_teams_rows(seeded):
    """Gate 3, proven non-vacuously: TEAM_B is checked and genuinely has zero."""
    result = propose_batch(TEAM_A, A1, [_task_create("only team A")])
    batch_id = result["batch_id"]

    conn = _admin()
    try:
        count_a = conn.execute(
            "select count(*) from public.consent_queue where batch_id=%s and team_id=%s",
            (batch_id, TEAM_A),
        ).fetchone()[0]
        count_b = conn.execute(
            "select count(*) from public.consent_queue where batch_id=%s and team_id=%s",
            (batch_id, TEAM_B),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count_a == 1
    assert count_b == 0


# ---------------------------------------------------------------------------
# THE independence test -- the point of the whole task (gate 2).
# ---------------------------------------------------------------------------

def test_approving_one_and_rejecting_another_leaves_the_third_pending_and_approvable(seeded):
    result = propose_batch(TEAM_A, A1, [
        _task_create("Task one"),
        _task_create("Task two"),
        _task_create("Task three"),
    ])
    c1, c2, c3 = (item["consent_id"] for item in result["items"])

    approved = approve_consent(TEAM_A, c1, A1)
    rejected = reject_consent(TEAM_A, c2, A1, reason="not needed")

    assert approved["status"] == "executed"
    assert rejected["status"] == "rejected"

    conn = _admin()
    try:
        _batch_id, status_three = _row(conn, c3)
    finally:
        conn.close()
    assert status_three == "pending"

    # Not just untouched -- genuinely still approvable, the actual invariant.
    still_works = approve_consent(TEAM_A, c3, A1)
    assert still_works["status"] == "executed"


def test_rejecting_the_first_of_three_does_not_cancel_its_siblings(seeded):
    """The forbidden shape, checked from the other direction: one rejection
    must never cascade into the rest of the batch."""
    result = propose_batch(TEAM_A, A1, [
        _task_create("Keep me pending A"),
        _task_create("Keep me pending B"),
        _task_create("Reject me"),
    ])
    c1, c2, c3 = (item["consent_id"] for item in result["items"])

    reject_consent(TEAM_A, c3, A1)

    conn = _admin()
    try:
        status_one = _row(conn, c1)[1]
        status_two = _row(conn, c2)[1]
    finally:
        conn.close()
    assert status_one == "pending"
    assert status_two == "pending"


# ---------------------------------------------------------------------------
# Partial failure: best-effort, not atomic.
# ---------------------------------------------------------------------------

def test_an_invalid_item_fails_on_its_own_without_sinking_the_rest(seeded):
    result = propose_batch(TEAM_A, A1, [
        _task_create("Good one"),
        {"tool_name": "no_such_tool", "args": {"text": "hi"}},
        _task_create("Also good"),
    ])

    statuses = [item["status"] for item in result["items"]]
    assert statuses == ["pending", "failed", "pending"]
    assert "error" in result["items"][1]
    assert "consent_id" not in result["items"][1]

    good_ids = [result["items"][0]["consent_id"], result["items"][2]["consent_id"]]
    conn = _admin()
    try:
        rows = [_row(conn, cid) for cid in good_ids]
    finally:
        conn.close()
    assert all(batch_id == result["batch_id"] for batch_id, _status in rows)
    assert all(status == "pending" for _batch_id, status in rows)


def test_propose_batch_rejects_an_empty_list(seeded):
    with pytest.raises(ValueError):
        propose_batch(TEAM_A, A1, [])


# ---------------------------------------------------------------------------
# The ADK wrapper binds ids from session state, never from model arguments
# (same rule test_task_tools.py proves for task_propose_update).
# ---------------------------------------------------------------------------

def test_the_batch_wrapper_binds_ids_from_state_not_arguments(seeded):
    result = team_propose_batch(
        [_task_create("from the wrapper")],
        _ctx(team_id=TEAM_A, requester_id=A1),
    )
    consent_id = result["items"][0]["consent_id"]

    conn = _admin()
    try:
        requester_id, team_id = conn.execute(
            "select requesting_member_id, team_id from public.consent_queue where id=%s",
            (consent_id,),
        ).fetchone()
    finally:
        conn.close()
    assert str(requester_id) == A1
    assert str(team_id) == TEAM_A
