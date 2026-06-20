"""Data-layer invariant checks (0003 triggers): task confirm guard,
membership role guard, and change_log audit."""
import psycopg
import pytest
from psycopg import errors

from shared.config import settings
from shared.db import Role, team_session
from tests._seed import A1, A2, TEAM_A, as_user


def _make_task(assignee, status="proposed"):
    """Create a task via admin (owner; triggers still fire). Returns its id."""
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        return conn.execute(
            "insert into public.tasks (team_id, assignee_id, title, status,"
            " created_by_kind, created_by_id) values (%s, %s, 't', %s, 'user', %s)"
            " returning id",
            (TEAM_A, assignee, status, A1),
        ).fetchone()[0]
    finally:
        conn.close()


# ---------- task confirm guard ----------

def test_new_task_must_start_proposed(seeded):
    with pytest.raises(errors.RaiseException):
        with as_user(A1) as conn:
            conn.execute(
                "insert into public.tasks (team_id, assignee_id, title, status,"
                " created_by_kind, created_by_id)"
                " values (%s, %s, 't', 'confirmed', 'user', %s)",
                (TEAM_A, A2, A1),
            )


def test_assignee_can_confirm_own_task(seeded):
    tid = _make_task(A2)
    with as_user(A2) as conn:  # the assignee
        conn.execute(
            "update public.tasks set status='confirmed', confirmed_at=now()"
            " where id=%s",
            (tid,),
        )  # no error == allowed


def test_non_assignee_cannot_confirm(seeded):
    tid = _make_task(A2)
    with pytest.raises(errors.RaiseException):
        with as_user(A1) as conn:  # leader, but not the assignee
            conn.execute(
                "update public.tasks set status='confirmed', confirmed_at=now()"
                " where id=%s",
                (tid,),
            )


def test_reassignment_resets_confirmation(seeded):
    tid = _make_task(A2)
    with as_user(A2, commit=True) as conn:
        conn.execute(
            "update public.tasks set status='confirmed', confirmed_at=now()"
            " where id=%s",
            (tid,),
        )
    with as_user(A1, commit=True) as conn:  # reassign to A1
        conn.execute("update public.tasks set assignee_id=%s where id=%s", (A1, tid))
    chk = psycopg.connect(settings.comrade_db_url_admin)
    try:
        row = chk.execute(
            "select status, confirmed_at from public.tasks where id=%s", (tid,)
        ).fetchone()
    finally:
        chk.close()
    assert row[0] == "proposed" and row[1] is None


# ---------- membership role guard ----------

def test_member_cannot_self_promote(seeded):
    with pytest.raises(errors.RaiseException):
        with as_user(A2) as conn:
            conn.execute(
                "update public.memberships set role='leader'"
                " where team_id=%s and user_id=%s",
                (TEAM_A, A2),
            )


def test_leader_can_promote_another(seeded):
    with as_user(A1) as conn:  # A1 is leader
        conn.execute(
            "update public.memberships set role='leader'"
            " where team_id=%s and user_id=%s",
            (TEAM_A, A2),
        )  # no error == allowed


def test_agent_cannot_change_roles(seeded):
    # blocked at the GRANT layer (agent has no UPDATE on memberships) before the guard
    with pytest.raises(errors.InsufficientPrivilege):
        with team_session(Role.AGENT, TEAM_A) as conn:
            conn.execute(
                "update public.memberships set role='leader'"
                " where team_id=%s and user_id=%s",
                (TEAM_A, A2),
            )


# ---------- audit / change_log ----------

def test_audit_records_user_actor(seeded):
    with as_user(A1) as conn:
        tid = conn.execute(
            "insert into public.tasks (team_id, assignee_id, title,"
            " created_by_kind, created_by_id) values (%s, %s, 't', 'user', %s)"
            " returning id",
            (TEAM_A, A2, A1),
        ).fetchone()[0]
        actor_kind, actor_id, action = conn.execute(
            "select actor_kind, actor_id, action from public.change_log"
            " where table_name='tasks' and row_id=%s",
            (tid,),
        ).fetchone()
    assert actor_kind == "user" and str(actor_id) == A1 and action == "create"


def test_audit_records_worker_actor(seeded):
    # the executor is the worker that writes tasks (agent's task-write was revoked)
    with team_session(Role.EXECUTOR, TEAM_A) as conn:
        tid = conn.execute(
            "insert into public.tasks (team_id, assignee_id, title, created_by_kind)"
            " values (%s, %s, 't', 'ai') returning id",
            (TEAM_A, A2),
        ).fetchone()[0]
    # executor has no change_log SELECT; read the audit row via admin
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        actor_kind, actor_id = conn.execute(
            "select actor_kind, actor_id from public.change_log"
            " where table_name='tasks' and row_id=%s",
            (tid,),
        ).fetchone()
    finally:
        conn.close()
    assert actor_kind == "ai" and actor_id is None
