"""A rejection reaches the model (§9.3 G3).

Today reject_consent sets a status and returns; the turn already ended, so the
agent never learns it was rejected or why, and proposes the same thing again.
This closes the loop: the reason is written to resolution_reason and rendered
into the next turn's instruction, read as the requesting member (findings
§4.1) -- comrade_agent has no SELECT on consent_queue at all.
"""
from types import SimpleNamespace

import psycopg

from agent.agent import build_instruction, recent_rejections
from shared.config import settings
from shared.consent import propose_action, reject_consent
from tests._seed import A1, A2, TEAM_A, TEAM_B, as_user, count


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _propose(team_id=TEAM_A, requester=A1, title="Write tests"):
    return propose_action(
        team_id, requester, "task_create",
        {"assignee_id": A2, "title": title, "description": None, "deadline": None},
    )["consent_id"]


def _ctx(team_id, requester_id):
    """A stand-in for ADK's ReadonlyContext: build_instruction only ever reads
    ctx.state[...], so a real InvocationContext would be pure ceremony here."""
    return SimpleNamespace(state={"team_id": team_id, "requester_id": requester_id})


# ---------------------------------------------------------------------------
# reject_consent writes the reason
# ---------------------------------------------------------------------------

def test_reject_writes_the_reason_to_resolution_reason(seeded):
    cid = _propose()
    result = reject_consent(
        TEAM_A, cid, A1, reason="we already decided this in standup"
    )
    assert result["status"] == "rejected"

    conn = _admin()
    try:
        reason = conn.execute(
            "select resolution_reason from public.consent_queue where id=%s",
            (cid,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert reason == "we already decided this in standup"


def test_reject_with_no_reason_still_works(seeded):
    """The default keeps every existing caller (server/app.py, other tests)
    working unchanged."""
    cid = _propose()
    assert reject_consent(TEAM_A, cid, A1)["status"] == "rejected"

    conn = _admin()
    try:
        reason = conn.execute(
            "select resolution_reason from public.consent_queue where id=%s",
            (cid,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert reason is None


# ---------------------------------------------------------------------------
# The headline case: the model must see WHY, not just THAT.
# ---------------------------------------------------------------------------

def test_rejection_reason_appears_in_the_instruction(seeded):
    cid = _propose(title="Set up CI")
    reject_consent(TEAM_A, cid, A1, reason="we already decided this in standup")

    instruction = build_instruction(_ctx(TEAM_A, A1))
    assert "we already decided this in standup" in instruction


def test_recent_rejections_is_empty_with_nothing_rejected(seeded):
    assert recent_rejections(TEAM_A, A1) == ""


def test_approved_and_pending_items_do_not_appear(seeded):
    """Only status='rejected' belongs here -- an approved or still-pending
    proposal is not a 'no' the agent needs to remember."""
    from shared.consent import approve_consent

    approved_id = _propose(title="approved one")
    approve_consent(TEAM_A, approved_id, A1)
    _propose(title="still pending")

    section = recent_rejections(TEAM_A, A1)
    assert "approved one" not in section
    assert "still pending" not in section


# ---------------------------------------------------------------------------
# The single most likely way to get this wrong: a missing team_id filter.
# `au_consent_queue_select` scopes by requesting_member_id only -- under
# `authenticated` there is no current_team(), so a member's rejection in one
# team is otherwise indistinguishable from their rejection in another.
# ---------------------------------------------------------------------------

def test_a_rejection_in_one_team_does_not_leak_into_anothers_instruction(seeded):
    conn = _admin()
    try:
        # A2 is already a TEAM_A member (seed); add them to TEAM_B too, so
        # this is a real dual-membership case, not a vacuous one.
        conn.execute(
            "insert into public.memberships (team_id, user_id, role, status)"
            " values (%s, %s, 'member', 'active')",
            (TEAM_B, A2),
        )
    finally:
        conn.close()

    cid = _propose(team_id=TEAM_B, requester=A2, title="rewrite the whole backend")
    reject_consent(TEAM_B, cid, A2, reason="scope creep, not doing this")

    # Prove A2 really CAN see the TEAM_B row under `authenticated` -- if this
    # assertion fails, the later "not in" assertion below would be vacuous.
    with as_user(A2) as conn:
        seen = count(
            conn,
            "select count(*) from public.consent_queue"
            " where id=%s and status='rejected'",
            (cid,),
        )
    assert seen == 1

    team_a_section = recent_rejections(TEAM_A, A2)
    assert "scope creep" not in team_a_section
    assert "rewrite the whole backend" not in team_a_section

    team_a_instruction = build_instruction(_ctx(TEAM_A, A2))
    assert "scope creep" not in team_a_instruction
    assert "rewrite the whole backend" not in team_a_instruction

    # Sanity: TEAM_B's own instruction for the same member DOES carry it.
    team_b_instruction = build_instruction(_ctx(TEAM_B, A2))
    assert "scope creep, not doing this" in team_b_instruction


# ---------------------------------------------------------------------------
# Bounds: a handful of recent items, not the full history.
# ---------------------------------------------------------------------------

def test_only_the_most_recent_five_are_shown(seeded):
    conn = _admin()
    try:
        for i in range(7):
            cid = _propose(title=f"proposal {i}")
            reject_consent(TEAM_A, cid, A1, reason=f"reason {i}")
        # Force a deterministic order -- proposals made in the same test can
        # share a timestamp at second resolution.
        for i in range(7):
            conn.execute(
                "update public.consent_queue set resolved_at ="
                " now() - (%s || ' seconds')::interval"
                " where team_id=%s and status='rejected'"
                " and tool_args->>'title'=%s",
                (str(6 - i), TEAM_A, f"proposal {i}"),
            )
    finally:
        conn.close()

    section = recent_rejections(TEAM_A, A1)
    # newest 5 are proposals 2..6 (0 and 1 pushed out by the cap)
    for i in range(2, 7):
        assert f"proposal {i}" in section
    assert "proposal 0" not in section
    assert "proposal 1" not in section


def test_a_rejection_older_than_the_window_does_not_appear(seeded):
    cid = _propose(title="ancient history")
    reject_consent(TEAM_A, cid, A1, reason="long resolved")
    conn = _admin()
    try:
        conn.execute(
            "update public.consent_queue set resolved_at = now() - interval '30 days'"
            " where id=%s",
            (cid,),
        )
    finally:
        conn.close()

    assert "ancient history" not in recent_rejections(TEAM_A, A1)
