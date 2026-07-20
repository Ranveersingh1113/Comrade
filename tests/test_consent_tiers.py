"""Blast-radius tiers: floors, the T3 two-key gate, and countersign integrity."""
import psycopg
import pytest

from shared.config import settings
from shared.consent import (
    ConsentError, add_second_key, approve_consent, execute_consent,
    propose_action, resolve_tier,
)
from tests._seed import A1, A2, TEAM_A, as_user


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _propose_t3(body="External post"):
    return propose_action(
        TEAM_A, A1, "post_group_message", {"body": body},
        source_snippet="asked in chat", tier="T3",
    )["consent_id"]


# ---------- tier resolution (pure) ----------

def test_tier_floors_cannot_be_lowered():
    assert resolve_tier("post_group_message", "T0") == "T2"
    assert resolve_tier("task_create", None) == "T1"


def test_tier_can_be_raised():
    assert resolve_tier("task_create", "T3") == "T3"


def test_unknown_tool_defaults_conservatively():
    assert resolve_tier("someday_send_money", None) == "T2"
    assert resolve_tier("someday_send_money", "garbage") == "T2"


# ---------- the T3 gate ----------

def test_t3_without_second_key_does_not_execute(seeded):
    cid = _propose_t3()
    out = approve_consent(TEAM_A, cid, A1)
    assert out == {"status": "approved", "awaiting": "second_key"}
    conn = _admin()
    try:
        n = conn.execute(
            "select count(*) from public.messages where team_id=%s"
            " and sender_kind='ai' and body='External post'", (TEAM_A,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 0


def test_t3_executes_once_both_keys_land(seeded):
    cid = _propose_t3("Two keys turned")
    approve_consent(TEAM_A, cid, A1)
    out = add_second_key(TEAM_A, cid, A2)
    assert out["status"] == "executed"


def test_t3_second_key_before_approval_waits(seeded):
    cid = _propose_t3("Countersigned first")
    out = add_second_key(TEAM_A, cid, A2)
    assert out == {"status": "countersigned", "awaiting": "requester_approval"}
    # requester's approval is now the last key
    assert approve_consent(TEAM_A, cid, A1)["status"] == "executed"


def test_direct_execute_still_blocked_without_second_key(seeded):
    """The gate lives in execute, not just the approve wrapper."""
    cid = _propose_t3()
    conn = _admin()
    try:
        conn.execute(
            "update public.consent_queue set status='approved' where id=%s",
            (cid,),
        )
    finally:
        conn.close()
    with pytest.raises(ConsentError, match="second key"):
        execute_consent(TEAM_A, cid)
    conn = _admin()
    try:
        status = conn.execute(
            "select status from public.consent_queue where id=%s", (cid,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert status == "approved"  # claim rolled back, retryable


# ---------- countersign integrity (RLS + trigger) ----------

def test_requester_cannot_countersign_their_own_item(seeded):
    cid = _propose_t3()
    out = add_second_key(TEAM_A, cid, A1)
    assert out["status"] == "not_found"


def test_requester_cannot_forge_the_second_key_column(seeded):
    cid = _propose_t3()
    with pytest.raises(psycopg.errors.RaiseException, match="cannot set"):
        with as_user(A1) as conn:
            conn.execute(
                "update public.consent_queue set second_approver_id=%s"
                " where id=%s", (A2, cid),
            )


def test_countersigner_cannot_touch_anything_else(seeded):
    cid = _propose_t3()
    with pytest.raises(psycopg.errors.RaiseException, match="only set the second key"):
        with as_user(A2) as conn:
            conn.execute(
                "update public.consent_queue set second_approver_id=%s,"
                " tool_args=%s::jsonb where id=%s",
                (A2, '{"body":"swapped payload"}', cid),
            )


def test_editing_args_voids_an_existing_countersign(seeded):
    """The second key blessed what the teammate SAW, not what it became."""
    cid = _propose_t3()
    add_second_key(TEAM_A, cid, A2)
    with as_user(A1, commit=True) as conn:
        conn.execute(
            "update public.consent_queue set tool_args=%s::jsonb,"
            " action_hash='restamped' where id=%s",
            ('{"body":"different action"}', cid),
        )
    conn = _admin()
    try:
        second = conn.execute(
            "select second_approver_id from public.consent_queue where id=%s",
            (cid,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert second is None


def test_teammates_can_see_pending_t3_items(seeded):
    cid = _propose_t3()
    with as_user(A2) as conn:
        row = conn.execute(
            "select tier from public.consent_queue where id=%s", (cid,)
        ).fetchone()
    assert row == ("T3",)


def test_teammates_still_cannot_see_lower_tier_items(seeded):
    cid = propose_action(
        TEAM_A, A1, "post_group_message", {"body": "T2 stays private"},
    )["consent_id"]
    with as_user(A2) as conn:
        row = conn.execute(
            "select 1 from public.consent_queue where id=%s", (cid,)
        ).fetchone()
    assert row is None


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
            "with a as (insert into public.messages (team_id, thread_type,"
            " sender_kind, body) values (%s,'group','ai','obs') returning id),"
            " u as (insert into public.messages (team_id, thread_type,"
            " sender_kind, sender_id, body) values (%s,'group','user',%s,'hi')"
            " returning id) select a.id, u.id from a, u",
            (TEAM_A, TEAM_A, A1),
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
