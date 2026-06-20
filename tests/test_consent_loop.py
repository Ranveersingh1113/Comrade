"""The closed approval loop: a human approves/rejects/edits a proposal and the
gated action executes (or doesn't). Authorization is RLS (requester-only)."""
import psycopg

from shared.config import settings
from shared.consent import (
    approve_consent, edit_and_approve, execute_consent, propose_action, reject_consent,
)
from tests._seed import A1, A2, TEAM_A


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _task_count(conn, title):
    return conn.execute(
        "select count(*) from public.tasks where team_id=%s and title=%s",
        (TEAM_A, title),
    ).fetchone()[0]


def _propose(requester=A1, title="Ship it"):
    return propose_action(
        TEAM_A, requester, "task_create",
        {"assignee_id": A2, "title": title, "description": None, "deadline": None},
    )["consent_id"]


def test_requester_approve_executes(seeded):
    cid = _propose()
    result = approve_consent(TEAM_A, cid, approver_id=A1)
    assert result["status"] == "executed"
    conn = _admin()
    try:
        assert _task_count(conn, "Ship it") == 1
        assert conn.execute(
            "select status from public.consent_queue where id=%s", (cid,)
        ).fetchone()[0] == "executed"
    finally:
        conn.close()


def test_non_requester_cannot_approve(seeded):
    cid = _propose(requester=A1)
    result = approve_consent(TEAM_A, cid, approver_id=A2)  # not the requester
    assert result["status"] == "not_approved"
    conn = _admin()
    try:
        assert _task_count(conn, "Ship it") == 0
        assert conn.execute(
            "select status from public.consent_queue where id=%s", (cid,)
        ).fetchone()[0] == "pending"  # untouched
    finally:
        conn.close()


def test_reject_then_execute_is_noop(seeded):
    cid = _propose()
    assert reject_consent(TEAM_A, cid, approver_id=A1)["status"] == "rejected"
    # even a direct execute can't run a rejected item
    assert execute_consent(TEAM_A, cid)["status"] == "noop"
    conn = _admin()
    try:
        assert _task_count(conn, "Ship it") == 0
    finally:
        conn.close()


def test_propose_group_message_approve_posts(seeded):
    cid = propose_action(
        TEAM_A, A1, "post_group_message", {"body": "Standup at 5pm."},
    )["consent_id"]
    conn = _admin()
    try:
        # nothing posted yet
        assert conn.execute(
            "select count(*) from public.messages where team_id=%s"
            " and thread_type='group' and sender_kind='ai'",
            (TEAM_A,),
        ).fetchone()[0] == 0
    finally:
        conn.close()

    assert approve_consent(TEAM_A, cid, approver_id=A1)["status"] == "executed"

    conn = _admin()
    try:
        posted = conn.execute(
            "select body from public.messages where team_id=%s"
            " and thread_type='group' and sender_kind='ai'",
            (TEAM_A,),
        ).fetchall()
        assert [r[0] for r in posted] == ["Standup at 5pm."]
    finally:
        conn.close()


def test_edit_and_approve_executes_new_args(seeded):
    cid = _propose(title="Old")
    result = edit_and_approve(
        TEAM_A, cid, approver_id=A1,
        new_args={"assignee_id": A2, "title": "Edited", "description": None, "deadline": None},
    )
    assert result["status"] == "executed"
    conn = _admin()
    try:
        assert _task_count(conn, "Edited") == 1
        assert _task_count(conn, "Old") == 0
    finally:
        conn.close()
