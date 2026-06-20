"""Consent mechanism: propose -> approve -> execute, with CAS, hash, expiry,
preconditions, and the edit path."""
import psycopg
import pytest

from shared.config import settings
from shared.consent import ConsentError, compute_hash, execute_consent, propose_action
from tests._seed import A1, A2, TEAM_A


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _set(conn, consent_id, **cols):
    sets = ", ".join(f"{k}=%s" for k in cols)
    conn.execute(
        f"update public.consent_queue set {sets} where id=%s",
        (*cols.values(), consent_id),
    )


def _task_count(conn, title):
    return conn.execute(
        "select count(*) from public.tasks where team_id=%s and title=%s",
        (TEAM_A, title),
    ).fetchone()[0]


def _propose(title="Write tests"):
    return propose_action(
        TEAM_A, A1, "task_create",
        {"assignee_id": A2, "title": title, "description": None, "deadline": None},
        source_snippet="from the Tuesday thread",
    )["consent_id"]


def test_propose_writes_pending_without_acting(seeded):
    cid = _propose()
    conn = _admin()
    try:
        status = conn.execute(
            "select status from public.consent_queue where id=%s", (cid,)
        ).fetchone()[0]
        assert status == "pending"
        assert _task_count(conn, "Write tests") == 0  # nothing created yet
    finally:
        conn.close()


def test_approve_then_execute_creates_task(seeded):
    cid = _propose()
    conn = _admin()
    try:
        _set(conn, cid, status="approved")
    finally:
        conn.close()

    result = execute_consent(TEAM_A, cid)
    assert result["status"] == "executed"

    conn = _admin()
    try:
        assert _task_count(conn, "Write tests") == 1
        assert conn.execute(
            "select status from public.consent_queue where id=%s", (cid,)
        ).fetchone()[0] == "executed"
    finally:
        conn.close()


def test_double_execute_is_noop(seeded):
    cid = _propose()
    conn = _admin()
    try:
        _set(conn, cid, status="approved")
    finally:
        conn.close()

    first = execute_consent(TEAM_A, cid)
    second = execute_consent(TEAM_A, cid)  # CAS: nothing left to claim
    assert first["status"] == "executed"
    assert second["status"] == "noop"

    conn = _admin()
    try:
        assert _task_count(conn, "Write tests") == 1  # exactly one task
    finally:
        conn.close()


def test_wrong_status_is_noop(seeded):
    cid = _propose()  # still pending (not approved)
    result = execute_consent(TEAM_A, cid)
    assert result["status"] == "noop"
    conn = _admin()
    try:
        assert _task_count(conn, "Write tests") == 0
    finally:
        conn.close()


def test_hash_mismatch_rejected(seeded):
    cid = _propose()
    conn = _admin()
    try:
        # tamper with args WITHOUT re-stamping the hash, then approve
        conn.execute(
            "update public.consent_queue set tool_args="
            " jsonb_set(tool_args, '{title}', '\"Hijacked\"'), status='approved'"
            " where id=%s",
            (cid,),
        )
    finally:
        conn.close()

    with pytest.raises(ConsentError):
        execute_consent(TEAM_A, cid)

    conn = _admin()
    try:
        # claim rolled back -> still approved, nothing created
        assert conn.execute(
            "select status from public.consent_queue where id=%s", (cid,)
        ).fetchone()[0] == "approved"
        assert _task_count(conn, "Hijacked") == 0
    finally:
        conn.close()


def test_expired_rejected(seeded):
    cid = _propose()
    conn = _admin()
    try:
        _set(conn, cid, status="approved")
        conn.execute(
            "update public.consent_queue set expires_at = now() - interval '1 day'"
            " where id=%s",
            (cid,),
        )
    finally:
        conn.close()

    with pytest.raises(ConsentError):
        execute_consent(TEAM_A, cid)


def test_edited_executes_with_new_args(seeded):
    cid = _propose(title="Old title")
    new_args = {"assignee_id": A2, "title": "New title", "description": None, "deadline": None}
    new_hash = compute_hash("task_create", TEAM_A, A1, new_args)
    conn = _admin()
    try:
        from psycopg.types.json import Json
        conn.execute(
            "update public.consent_queue set tool_args=%s, action_hash=%s,"
            " status='edited' where id=%s",
            (Json(new_args), new_hash, cid),
        )
    finally:
        conn.close()

    result = execute_consent(TEAM_A, cid)
    assert result["status"] == "executed"
    conn = _admin()
    try:
        assert _task_count(conn, "New title") == 1
        assert _task_count(conn, "Old title") == 0
    finally:
        conn.close()
