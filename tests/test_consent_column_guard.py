"""A requester may change only what a decision needs, not the whole row.

Carried Important finding from Phase 0's whole-branch review.
`au_consent_queue_update` restricts which ROWS a member may update and nothing
else, so the requester could rewrite any column on their own pending row:
rewind `status` to 'pending' and re-approve to execute a second time, or
rewrite `team_id`, `action_hash`, `tool_args` or `tier`.

Exactly-once held against retries, races and the agent. It did not hold
against the requester, which is the last hole in the consent invariant.
"""
import psycopg
import pytest

from shared.config import settings
from shared.consent import approve_consent, propose_action, reject_consent
from tests._seed import A1, A2, TEAM_A, TEAM_B, as_user


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _pending(**over):
    args = {"assignee_id": A1, "title": "write the report",
            "description": None, "deadline": None}
    args.update(over)
    return propose_action(TEAM_A, A1, "task_create", args)["consent_id"]


# ---------------------------------------------------------------------------
# The hole
# ---------------------------------------------------------------------------

def test_the_requester_cannot_rewind_a_resolved_item(seeded):
    """The double-execution path: resolve, rewind to pending, approve again."""
    cid = _pending()
    approve_consent(TEAM_A, cid, A1)

    with as_user(A1) as conn:
        with pytest.raises(psycopg.Error):
            conn.execute(
                "update public.consent_queue set status='pending',"
                " resolved_at=null where id=%s",
                (cid,),
            )


def test_the_requester_cannot_rewrite_the_action_hash(seeded):
    """action_hash binds {tool, team, requester, args}; rewriting it unbinds."""
    cid = _pending()
    with as_user(A1) as conn:
        with pytest.raises(psycopg.Error):
            conn.execute(
                "update public.consent_queue set action_hash='deadbeef'"
                " where id=%s",
                (cid,),
            )


def test_the_requester_cannot_move_an_item_to_another_team(seeded):
    cid = _pending()
    with as_user(A1) as conn:
        with pytest.raises(psycopg.Error):
            conn.execute(
                "update public.consent_queue set team_id=%s where id=%s",
                (TEAM_B, cid),
            )


def test_the_requester_cannot_lower_the_tier(seeded):
    cid = _pending()
    with as_user(A1) as conn:
        with pytest.raises(psycopg.Error):
            conn.execute(
                "update public.consent_queue set tier='T0' where id=%s", (cid,)
            )


def test_the_requester_cannot_reassign_the_proposal_to_someone_else(seeded):
    cid = _pending()
    with as_user(A1) as conn:
        with pytest.raises(psycopg.Error):
            conn.execute(
                "update public.consent_queue set requesting_member_id=%s"
                " where id=%s",
                (A2, cid),
            )


def test_the_requester_cannot_extend_their_own_expiry(seeded):
    cid = _pending()
    with as_user(A1) as conn:
        with pytest.raises(psycopg.Error):
            conn.execute(
                "update public.consent_queue"
                " set expires_at = now() + interval '365 days' where id=%s",
                (cid,),
            )


# ---------------------------------------------------------------------------
# The legitimate transitions must all still work
# ---------------------------------------------------------------------------

def test_approve_still_works(seeded):
    cid = _pending()
    assert approve_consent(TEAM_A, cid, A1)["status"] == "executed"


def test_reject_still_works(seeded):
    cid = _pending(title="something else")
    assert reject_consent(TEAM_A, cid, A1)["status"] == "rejected"


def test_edit_and_approve_still_rewrites_args_and_hash(seeded):
    """edit_and_approve is the ONE path that legitimately rewrites the hash."""
    from shared.consent import edit_and_approve

    cid = _pending(title="original title")
    out = edit_and_approve(
        TEAM_A, cid, A1,
        {"assignee_id": A1, "title": "edited title",
         "description": None, "deadline": None},
    )
    assert out["status"] == "executed"


def test_a_worker_role_passes_through_untouched(seeded):
    """The executor flips status to 'executed'; auth.uid() is null for it."""
    from shared.db import Role, team_session

    cid = _pending(title="worker path")
    with team_session(Role.EXECUTOR, TEAM_A) as conn:
        conn.execute(
            "update public.consent_queue set status='cancelled',"
            " resolved_at=now() where id=%s",
            (cid,),
        )
    conn = _admin()
    try:
        assert conn.execute(
            "select status from public.consent_queue where id=%s", (cid,)
        ).fetchone()[0] == "cancelled"
    finally:
        conn.close()
