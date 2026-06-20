"""Private nudges: immediate send + cooldown dedupe (no human gate)."""
import psycopg

from shared.config import settings
from shared.nudge import send_nudge
from tests._seed import A2, TEAM_A


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _private_count(conn):
    return conn.execute(
        "select count(*) from public.messages where team_id=%s"
        " and thread_type='private' and thread_owner_id=%s and sender_kind='ai'",
        (TEAM_A, A2),
    ).fetchone()[0]


def test_nudge_sends_immediately(seeded):
    result = send_nudge(TEAM_A, A2, "pending_task", subject="task-123")
    assert result["status"] == "sent"
    conn = _admin()
    try:
        assert _private_count(conn) == 1
        assert conn.execute(
            "select count(*) from public.nudge_log where member_id=%s", (A2,)
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_identical_nudge_within_cooldown_suppressed(seeded):
    first = send_nudge(TEAM_A, A2, "pending_task", subject="task-123")
    second = send_nudge(TEAM_A, A2, "pending_task", subject="task-123")
    assert first["status"] == "sent"
    assert second["status"] == "suppressed"
    conn = _admin()
    try:
        assert _private_count(conn) == 1  # only the first landed
    finally:
        conn.close()


def test_different_subject_not_suppressed(seeded):
    send_nudge(TEAM_A, A2, "pending_task", subject="task-123")
    other = send_nudge(TEAM_A, A2, "pending_task", subject="task-999")
    assert other["status"] == "sent"
    conn = _admin()
    try:
        assert _private_count(conn) == 2
    finally:
        conn.close()
