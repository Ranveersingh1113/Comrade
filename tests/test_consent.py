"""Consent mechanism: propose -> approve -> execute, with CAS, hash, expiry,
preconditions, and the edit path."""
import psycopg
import pytest

from shared.config import settings
from shared.consent import ConsentError, compute_hash, execute_consent, propose_action
from tests._seed import A1, A2, TEAM_A, TEAM_B


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


def test_identical_pending_proposal_returns_the_existing_one(seeded):
    """A retried turn must not produce two cards — nor kill the turn (§2.2).

    uq_consent_pending_hash exists to make the retry idempotent. Letting its
    UniqueViolation escape turned that guarantee into a 500 on the whole turn,
    which is the opposite of what the index is for.
    """
    args = {"assignee_id": A1, "title": "write the report",
            "description": None, "deadline": None}
    first = propose_action(TEAM_A, A1, "task_create", args)
    second = propose_action(TEAM_A, A1, "task_create", args)

    assert second["consent_id"] == first["consent_id"]
    assert second["status"] == "pending"
    assert second["tier"] == first["tier"]
    conn = _admin()
    try:
        rows = conn.execute(
            "select count(*) from public.consent_queue where team_id=%s"
            " and action_hash=%s",
            (TEAM_A, first["action_hash"]),
        ).fetchone()[0]
    finally:
        conn.close()
    assert rows == 1


def test_proposing_a_tool_with_no_executor_is_refused_at_propose_time():
    """§13.5: a proposal naming an unexecutable tool must never be queued.

    Today it is written happily and fails only when a human approves it — the
    worst possible moment to discover it. This is also what would have turned
    Phase 0's post_group_message removal into a red suite instead of a green
    one.
    """
    with pytest.raises(ConsentError, match="no executor"):
        propose_action(TEAM_A, A1, "post_group_message", {"text": "hi"})


# ---------------------------------------------------------------------------
# The team the caller names is not the team the row belongs to
# ---------------------------------------------------------------------------
# Two policies, and between them a seam:
#
#   au_consent_queue_update   requesting_member_id = auth.uid()   <- no team
#   ex_consent_queue          team_id = current_team()            <- no owner
#
# The requester side is team-blind. So every resolution function took a
# team_id from its caller, filtered the row by id alone, and then handed that
# unverified team_id to execute_consent, which opens the executor session with
# it. Nothing ever asked whether the row was in that team.
#
# server/app.py calls require_membership(user_id, req.team_id) first, so the
# caller does have to belong to the team they name — it just need not be the
# team the consent row is in. Anyone in two teams can reach this, and so can
# an honest client that sends the wrong id.

def test_approving_with_another_teams_id_does_not_resolve_the_row(seeded):
    """The row must not move. Today it lands in 'approved' and stays there:
    the requester-side update matches on id alone and succeeds, and then
    execute_consent finds nothing under the other team's session and returns
    a noop — leaving a row that was never executed marked as approved."""
    from shared.consent import approve_consent

    consent_id = _propose("wrong team on approve")
    result = approve_consent(TEAM_B, consent_id, A1)

    conn = _admin()
    try:
        status = conn.execute(
            "select status from public.consent_queue where id=%s", (consent_id,)
        ).fetchone()[0]
        assert status == "pending", (
            f"a consent row in team A moved to {status!r} on a call naming"
            " team B"
        )
        assert _task_count(conn, "wrong team on approve") == 0
    finally:
        conn.close()
    assert result["status"] != "executed"


def test_rejecting_with_another_teams_id_does_not_resolve_the_row(seeded):
    """reject_consent never used its team_id at all — the parameter was
    decorative, and the row was rejected on the strength of ownership alone."""
    from shared.consent import reject_consent

    consent_id = _propose("wrong team on reject")
    reject_consent(TEAM_B, consent_id, A1, "no")

    conn = _admin()
    try:
        status = conn.execute(
            "select status from public.consent_queue where id=%s", (consent_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert status == "pending", (
        f"a consent row in team A was {status!r} by a call naming team B"
    )


def test_editing_with_another_teams_id_does_not_brick_the_row(seeded):
    """🔴 The one that does lasting damage.

    edit_and_approve recomputes the action hash — and it bound it to the
    team_id the CALLER passed, not the team the row is in. A wrong id
    therefore wrote a hash bound to the other team and flipped the row to
    'edited'. The execute that follows finds nothing under that team, so the
    row survives carrying a hash it can never match: a later, correct call
    computes the hash with the real team, gets a mismatch, and raises. The
    item is permanently unexecutable and nothing says why.

    Assert on the hash, not just the status — the status is recoverable and
    the hash is not.
    """
    from shared.consent import edit_and_approve

    consent_id = _propose("wrong team on edit")
    conn = _admin()
    try:
        before = conn.execute(
            "select action_hash from public.consent_queue where id=%s",
            (consent_id,),
        ).fetchone()[0]
        edit_and_approve(
            TEAM_B, consent_id, A1,
            {"assignee_id": A2, "title": "edited", "description": None,
             "deadline": None},
        )
        after, status = conn.execute(
            "select action_hash, status from public.consent_queue where id=%s",
            (consent_id,),
        ).fetchone()
    finally:
        conn.close()

    assert after == before, (
        "a call naming the wrong team rewrote the action hash. The row can no"
        " longer be executed by anyone: the stored hash is bound to a team the"
        " row is not in."
    )
    assert status == "pending", f"row moved to {status!r}"
