"""Blast-radius tiers: floors, and the invariant that T3 no longer resolves."""
import psycopg
import pytest

from shared.config import settings
from shared.consent import propose_action, resolve_tier
from tests._seed import A1, A2, TEAM_A, as_user, general_thread


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


# ---------- tier resolution (pure) ----------

def test_tier_floors_cannot_be_lowered():
    assert resolve_tier("task_create", None) == "T1"


def test_tier_can_still_be_raised_above_its_floor():
    """The surviving half of the old T3 story (§10): a proposal may still ask
    for a tier above its tool's floor. Below the floor, the floor still wins —
    this is the one behaviour that justifies keeping the tier column at all."""
    assert resolve_tier("task_create", "T2") == "T2"
    assert resolve_tier("task_create", "T0") == "T1"


def test_unknown_tool_defaults_conservatively():
    assert resolve_tier("someday_send_money", None) == "T2"
    assert resolve_tier("someday_send_money", "garbage") == "T2"


# ---------- observation suppression + opens summary (DB) ----------

def test_suppression_rls_member_writes_own_only(seeded):
    with as_user(A1, commit=True) as conn:
        conn.execute(
            "insert into public.observation_suppressions (team_id, member_id, kind)"
            " values (%s,%s,'quiet_member_callout')", (TEAM_A, A1),
        )
    with as_user(A2) as conn:  # teammate sees the standing request
        n = conn.execute(
            "select count(*) from public.observation_suppressions where team_id=%s",
            (TEAM_A,),
        ).fetchone()[0]
    assert n == 1
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with as_user(A2) as conn:  # but cannot write it as someone else
            conn.execute(
                "insert into public.observation_suppressions"
                " (team_id, member_id, kind) values (%s,%s,'x')", (TEAM_A, A1),
            )


def test_opens_summary_counts_without_naming(seeded):
    conn = _admin()
    try:
        doc = conn.execute(
            "insert into public.documents (team_id, kind, filename)"
            " values (%s,'text','spec.txt') returning id", (TEAM_A,),
        ).fetchone()[0]
        conn.execute(
            "insert into public.document_opens (document_id, user_id)"
            " values (%s,%s)", (doc, A2),
        )
    finally:
        conn.close()

    with as_user(A1) as conn:  # A1 did NOT open it, yet sees the count
        row = conn.execute(
            "select opens_count, member_count from public.document_opens_summary"
            " where document_id=%s", (doc,),
        ).fetchone()
    assert row == (1, 2)

    from tests._seed import B1
    with as_user(B1) as conn:  # outsider sees nothing
        row = conn.execute(
            "select 1 from public.document_opens_summary where document_id=%s",
            (doc,),
        ).fetchone()
    assert row is None


def test_tombstone_fn_marks_only_ai_messages_and_is_agent_only(seeded):
    from shared.db import Role, team_session

    conn = _admin()
    try:
        ai_id, user_id = conn.execute(
            "with a as (insert into public.messages (team_id, thread_id,"
            " sender_kind, body) values (%s,%s,'ai','obs') returning id),"
            " u as (insert into public.messages (team_id, thread_id,"
            " sender_kind, sender_id, body) values (%s,%s,'user',%s,'hi')"
            " returning id) select a.id, u.id from a, u",
            (TEAM_A, general_thread(conn, TEAM_A), TEAM_A,
             general_thread(conn, TEAM_A), A1),
        ).fetchone()
    finally:
        conn.close()

    with team_session(Role.AGENT, TEAM_A) as conn:
        hit = conn.execute(
            "select public.tombstone_ai_message(%s,%s)", (ai_id, TEAM_A)
        ).fetchone()[0]
        assert hit is True
        miss = conn.execute(
            "select public.tombstone_ai_message(%s,%s)", (user_id, TEAM_A)
        ).fetchone()[0]
        assert miss is False  # human messages are untouchable

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with as_user(A1) as conn:
            conn.execute("select public.tombstone_ai_message(%s,%s)", (ai_id, TEAM_A))


def test_suppress_refuses_a_memory_diff_card(seeded):
    """Diff cards are notifications, not observations — silencing them would
    break the post-hoc transparency the memory model depends on."""
    from fastapi.testclient import TestClient

    from server.app import app
    from server.auth import current_user_id

    conn = _admin()
    try:
        msg_id = conn.execute(
            "insert into public.messages (team_id, thread_id, sender_kind, body)"
            " values (%s,%s,'ai','Memory updated — 2 added, 1 revised, 0 removed.')"
            " returning id",
            (TEAM_A, general_thread(conn, TEAM_A)),
        ).fetchone()[0]
        conn.execute(
            "insert into public.memory_compilations (team_id, trigger, status,"
            " diff_message_id) values (%s,'on_demand','done',%s)",
            (TEAM_A, msg_id),
        )
    finally:
        conn.close()

    app.dependency_overrides[current_user_id] = lambda: A1
    try:
        resp = TestClient(app).post(
            f"/observations/{msg_id}/suppress",
            json={"team_id": TEAM_A, "kind": "proactive_observation"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404
    conn = _admin()
    try:
        deleted, suppressions = conn.execute(
            "select (select deleted_at from public.messages where id=%(m)s),"
            " (select count(*) from public.observation_suppressions"
            "  where message_id=%(m)s)",
            {"m": msg_id},
        ).fetchone()
    finally:
        conn.close()
    assert deleted is None            # card still visible
    assert suppressions == 0          # nothing recorded


def test_suppress_still_works_on_a_plain_observation(seeded):
    from fastapi.testclient import TestClient

    from server.app import app
    from server.auth import current_user_id

    conn = _admin()
    try:
        msg_id = conn.execute(
            "insert into public.messages (team_id, thread_id, sender_kind, body)"
            " values (%s,%s,'ai','Observation: the doc has not moved.')"
            " returning id",
            (TEAM_A, general_thread(conn, TEAM_A)),
        ).fetchone()[0]
    finally:
        conn.close()

    app.dependency_overrides[current_user_id] = lambda: A1
    try:
        resp = TestClient(app).post(
            f"/observations/{msg_id}/suppress",
            json={"team_id": TEAM_A, "kind": "proactive_observation"},
        )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200


def test_tier_cannot_be_t3_any_more(seeded):
    """§10: T3 is gone. A caller asking for it gets the tool's floor instead."""
    assert resolve_tier("task_create", "T3") == "T1"
    cid = propose_action(
        TEAM_A, A1, "task_create",
        {"assignee_id": A1, "title": "x", "description": None, "deadline": None},
        tier="T3",
    )["consent_id"]
    conn = _admin()
    try:
        row = conn.execute(
            "select tier from public.consent_queue where id=%s", (cid,)
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "T1"
