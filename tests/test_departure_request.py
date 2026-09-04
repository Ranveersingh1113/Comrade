"""Asking a teammate to leave, when nobody is allowed to remove them.

D4's harder half. Leaving is easy — it is your own row
(tests/test_team_lifecycle.py). But teams do need a way to say "we think you
should go", and every obvious implementation of that is an admin power:
somebody gets a button that ejects somebody else, and §23.1 spends a section
explaining why this product does not have those.

The way out is the mechanism already here. A departure is filed as a consent
proposal whose `requesting_member_id` is **the person being asked** — and
au_consent_queue_update lets only the requester resolve their own items. So
the card appears in their inbox, the asker cannot even see it, and the only
key that turns is the one belonging to the person who would leave.

That makes the identity check load-bearing rather than decorative, and most of
this file is about the ways it could be got around.
"""
import psycopg
import pytest

from shared.config import settings
from shared.consent import (
    ConsentError, approve_consent, execute_consent, propose_action,
    reject_consent,
)
from shared.db import Role, team_session
from tests._seed import A1, A2, B1, TEAM_A, as_user


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def _ask(target, asker_note="A1 asked you to leave this team."):
    """What server/app.py's departure-request endpoint files."""
    return propose_action(
        team_id=TEAM_A,
        requester_id=target,
        tool_name="member_depart",
        args={"user_id": target},
        source_snippet=asker_note,
        reversible=False,
        tier="T1",
    )


# ---------------------------------------------------------------------------
# The happy path, both ways
# ---------------------------------------------------------------------------

def test_the_member_asked_can_agree_and_leaves(seeded, admin):
    proposal = _ask(A2)
    result = approve_consent(TEAM_A, proposal["consent_id"], A2)
    assert result["status"] == "executed"
    assert admin.execute(
        "select status from public.memberships where team_id=%s and user_id=%s",
        (TEAM_A, A2),
    ).fetchone()[0] == "left"


def test_the_member_asked_can_simply_say_no(seeded, admin):
    """And nothing happens. This is the case the whole design is for."""
    proposal = _ask(A2)
    assert reject_consent(
        TEAM_A, proposal["consent_id"], A2, reason="I am staying"
    )["status"] == "rejected"
    assert admin.execute(
        "select status from public.memberships where team_id=%s and user_id=%s",
        (TEAM_A, A2),
    ).fetchone()[0] == "active"


def test_the_card_is_a_T1_and_says_who_asked(seeded, admin):
    """T1 is 'affects one member' — the tier reasoning already in the schema.

    The note matters as much as the tier: a bare 'leave the team?' card with no
    author is the product asking you to go.
    """
    proposal = _ask(A2, "A1 asked you to leave this team. They said: scope changed")
    assert proposal["tier"] == "T1"
    row = admin.execute(
        "select source_snippet, reversible from public.consent_queue where id=%s",
        (proposal["consent_id"],),
    ).fetchone()
    assert "A1 asked you to leave" in row[0] and "scope changed" in row[0]
    assert row[1] is False


# ---------------------------------------------------------------------------
# The attack this shape exists to refuse
# ---------------------------------------------------------------------------

def test_the_asker_cannot_see_the_card_they_filed(seeded):
    """If they could see it they could approve it, and this would just be a
    removal button with extra steps."""
    proposal = _ask(A2)
    with as_user(A1) as conn:
        assert conn.execute(
            "select count(*) from public.consent_queue where id=%s",
            (proposal["consent_id"],),
        ).fetchone()[0] == 0


def test_a_member_cannot_file_a_departure_against_someone_else(seeded, admin):
    """The forgery that would turn consent into the admin power it replaces.

    A1 files a proposal naming A2 in the args but keeping the key for
    themselves, then approves it. The queue would happily hold such a row —
    nothing about `requesting_member_id` and `tool_args` has to agree — so the
    refusal has to be in the action itself, and it has to be checked at EXECUTE
    time rather than only at propose time.
    """
    forged = propose_action(
        team_id=TEAM_A,
        requester_id=A1,                 # the asker keeps the key…
        tool_name="member_depart",
        args={"user_id": A2},            # …but names their teammate
        tier="T1",
    )
    with pytest.raises(ConsentError, match="only be approved by the member leaving"):
        approve_consent(TEAM_A, forged["consent_id"], A1)
    assert admin.execute(
        "select status from public.memberships where team_id=%s and user_id=%s",
        (TEAM_A, A2),
    ).fetchone()[0] == "active"


def test_editing_the_args_cannot_redirect_a_departure(seeded, admin):
    """edit_and_approve re-stamps the hash, so the hash cannot catch this.

    A2 holds a card about their own departure and edits it to name A1 instead.
    The re-stamped hash makes it internally consistent — the identity check is
    the only thing standing between that and A2 removing a teammate.
    """
    from shared.consent import edit_and_approve

    proposal = _ask(A2)
    with pytest.raises(ConsentError, match="only be approved by the member leaving"):
        edit_and_approve(TEAM_A, proposal["consent_id"], A2, {"user_id": A1})
    assert admin.execute(
        "select status from public.memberships where team_id=%s and user_id=%s",
        (TEAM_A, A1),
    ).fetchone()[0] == "active"


def test_a_stranger_cannot_resolve_your_card(seeded, admin):
    proposal = _ask(A2)
    assert approve_consent(TEAM_A, proposal["consent_id"], B1)["status"] == "not_approved"
    assert admin.execute(
        "select status from public.memberships where team_id=%s and user_id=%s",
        (TEAM_A, A2),
    ).fetchone()[0] == "active"


def test_the_agent_cannot_propose_a_departure():
    """The invariant survives the tool that used to carry it.

    This was asserted through team_propose_batch, which was the one place a
    tool name chosen by the MODEL reached the consent queue. That tool was
    removed 2026-09-04, so the property now holds by construction — every
    remaining proposal tool hardcodes its own action name and the model
    cannot supply one.

    Held by construction is not the same as checked, and this is a security
    property, so it is re-anchored on the set itself rather than deleted with
    the tool. member_depart executes fine; the bar it fails is "should the
    MODEL be able to name this", because "Comrade suggests you leave the team"
    is not a card this product puts in anyone's inbox — and the pending-hash
    index means such a card would block the real one a teammate tried to send.
    """
    from shared.consent import AGENT_PROPOSABLE

    assert "member_depart" not in AGENT_PROPOSABLE
    assert AGENT_PROPOSABLE == {"task_create", "task_update", "repo_open_pr"}


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------

def test_a_card_left_lying_around_after_you_already_left(seeded, admin):
    """Two people ask; you leave on your own; the second card must not fire
    a second departure against a row that is no longer active."""
    proposal = _ask(A2)
    with as_user(A2, commit=True) as conn:
        conn.execute(
            "update public.memberships set status='left', left_at=now()"
            " where team_id=%s and user_id=%s",
            (TEAM_A, A2),
        )
    with pytest.raises(ConsentError, match="already not an active member"):
        approve_consent(TEAM_A, proposal["consent_id"], A2)


def test_asking_twice_reuses_the_one_card(seeded, admin):
    """uq_consent_pending_hash. Two teammates asking the same person is one
    question, not two — and it must not stack cards in their inbox."""
    first = _ask(A2)
    second = _ask(A2)
    assert first["consent_id"] == second["consent_id"]
    assert admin.execute(
        "select count(*) from public.consent_queue"
        " where team_id=%s and tool_name='member_depart' and status='pending'",
        (TEAM_A,),
    ).fetchone()[0] == 1


def test_the_executor_can_do_nothing_else_to_a_membership(seeded, admin):
    """ex_memberships_depart is the narrowest grant that lets the action work.

    comrade_executor gained UPDATE on memberships for this one transition. It
    must not be able to admit anyone, promote anyone, or reach another team —
    a zero-row update is the pass here, since a row the policy rejects is not
    there to write.
    """
    admin.execute(
        "insert into public.memberships (team_id, user_id, role, status)"
        " values (%s,%s,'member','invited')",
        (TEAM_A, B1),
    )
    with team_session(Role.EXECUTOR, TEAM_A) as conn:
        conn.execute(
            "update public.memberships set status='active'"
            " where team_id=%s and user_id=%s",
            (TEAM_A, B1),
        )
    assert admin.execute(
        "select status from public.memberships where team_id=%s and user_id=%s",
        (TEAM_A, B1),
    ).fetchone()[0] == "invited", "the executor admitted a member"


def test_execute_is_still_exactly_once(seeded, admin):
    """The CAS claim, on this action too: a replayed execute is a no-op, not a
    second departure."""
    proposal = _ask(A2)
    approve_consent(TEAM_A, proposal["consent_id"], A2)
    assert execute_consent(TEAM_A, proposal["consent_id"])["status"] == "noop"
