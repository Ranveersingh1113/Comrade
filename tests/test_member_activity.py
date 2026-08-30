"""Who might be stuck — the signal the `idle` nudge never had.

findings §3.1: `contribution_v` has no recency signal, and the `idle` nudge
type "exists with no data source to trigger it". The suppression machinery, the
templates and the cooldown were all built; nothing could ever fire it.

Governance note (platform-findings memo, ruling 6): this answers "who might be
stuck", NOT "who is doing least". It returns recency facts per member, never a
ranking, and callers must not sort it by volume.
"""
import psycopg
import pytest

from agent.tools import fetch_member_activity
from shared.config import settings
from tests._seed import A1, A2, B1, TEAM_A, TEAM_B


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def test_the_view_exposes_recency_columns(seeded, admin):
    cols = {
        r[0]
        for r in admin.execute(
            "select column_name from information_schema.columns"
            " where table_schema='public' and table_name='contribution_v'"
        ).fetchall()
    }
    assert {"last_message_at", "last_task_at"} <= cols


def test_every_active_member_appears(seeded):
    rows = fetch_member_activity(TEAM_A, A1)
    assert {r["user_id"] for r in rows} == {A1, A2}


def test_a_recent_message_sets_the_signal(seeded, admin):
    """The seed gives A2 a group message; A1 has only a private one."""
    by_id = {r["user_id"]: r for r in fetch_member_activity(TEAM_A, A1)}
    assert by_id[A2]["last_message_at"] is not None
    # A1's seeded message is PRIVATE — recency is a group-room signal, because
    # a private thread with the AI is not evidence of team participation.
    assert by_id[A1]["last_message_at"] is None


def test_days_since_last_signal_counts_from_the_most_recent(seeded, admin):
    admin.execute(
        "insert into public.messages (team_id, thread_type, sender_kind,"
        " sender_id, body, created_at)"
        " values (%s,'group','user',%s,'old news', now() - interval '9 days')",
        (TEAM_A, A1),
    )
    by_id = {r["user_id"]: r for r in fetch_member_activity(TEAM_A, A1)}
    assert by_id[A1]["days_since_last_signal"] == 9


def test_a_member_with_no_signal_reports_none(seeded):
    by_id = {r["user_id"]: r for r in fetch_member_activity(TEAM_A, A1)}
    assert by_id[A1]["days_since_last_signal"] is None


def test_a_task_counts_as_a_signal(seeded, admin):
    admin.execute(
        "insert into public.tasks (team_id, assignee_id, title, created_by_kind,"
        " created_by_id, updated_at) values (%s,%s,'x','user',%s,"
        " now() - interval '2 days')",
        (TEAM_A, A1, A1),
    )
    by_id = {r["user_id"]: r for r in fetch_member_activity(TEAM_A, A1)}
    assert by_id[A1]["last_task_at"] is not None
    assert by_id[A1]["days_since_last_signal"] == 2


def test_it_never_reaches_another_team(seeded, admin):
    """A1 joins TEAM_B; TEAM_A's activity must still show only TEAM_A."""
    admin.execute(
        "insert into public.memberships (team_id, user_id, role, status)"
        " values (%s,%s,'member','active')",
        (TEAM_B, A1),
    )
    ids = {r["user_id"] for r in fetch_member_activity(TEAM_A, A1)}
    assert B1 not in ids
    assert ids == {A1, A2}
