"""The closed approval loop: a human approves/rejects/edits a proposal and the
gated action executes (or doesn't). Authorization is RLS (requester-only)."""
import psycopg

from shared.config import settings
from shared.consent import (
    approve_consent, edit_and_approve, execute_consent, propose_action, reject_consent,
)
from tests._seed import A1, A2, TEAM_A


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _task_count(conn, title):
    return conn.execute(
        "select count(*) from public.tasks where team_id=%s and title=%s",
        (TEAM_A, title),
    ).fetchone()[0]


def _propose(requester=A1, title="Ship it"):
    return propose_action(
        TEAM_A, requester, "task_create",
        {"assignee_id": A2, "title": title, "description": None, "deadline": None},
    )["consent_id"]


def test_requester_approve_executes(seeded):
    cid = _propose()
    result = approve_consent(TEAM_A, cid, approver_id=A1)
    assert result["status"] == "executed"
    conn = _admin()
    try:
        assert _task_count(conn, "Ship it") == 1
        assert conn.execute(
            "select status from public.consent_queue where id=%s", (cid,)
        ).fetchone()[0] == "executed"
    finally:
        conn.close()


def test_non_requester_cannot_approve(seeded):
    cid = _propose(requester=A1)
    result = approve_consent(TEAM_A, cid, approver_id=A2)  # not the requester
    assert result["status"] == "not_approved"
    conn = _admin()
    try:
        assert _task_count(conn, "Ship it") == 0
        assert conn.execute(
            "select status from public.consent_queue where id=%s", (cid,)
        ).fetchone()[0] == "pending"  # untouched
    finally:
        conn.close()


def test_reject_then_execute_is_noop(seeded):
    cid = _propose()
    assert reject_consent(TEAM_A, cid, approver_id=A1)["status"] == "rejected"
    # even a direct execute can't run a rejected item
    assert execute_consent(TEAM_A, cid)["status"] == "noop"
    conn = _admin()
    try:
        assert _task_count(conn, "Ship it") == 0
    finally:
        conn.close()


def test_edit_and_approve_executes_new_args(seeded):
    cid = _propose(title="Old")
    result = edit_and_approve(
        TEAM_A, cid, approver_id=A1,
        new_args={"assignee_id": A2, "title": "Edited", "description": None, "deadline": None},
    )
    assert result["status"] == "executed"
    conn = _admin()
    try:
        assert _task_count(conn, "Edited") == 1
        assert _task_count(conn, "Old") == 0
    finally:
        conn.close()


def test_the_agent_may_propose_exactly_three_things(seeded):
    """§13: the agent may not originate group content. Nothing executes it.

    This pinned `_EXECUTORS` because, until D4, "what can be executed" and
    "what the agent may ask for" were the same set — so one assertion covered
    both and nobody had to notice they were different questions. member_depart
    separated them: it executes, and the agent must never name it.

    So the pin moves to AGENT_PROPOSABLE, which is the set that actually
    bounds the model, and _EXECUTORS keeps only the assertion that is about
    the agent's reach rather than its size. If a future action is added to
    both sets, THIS test is the one that should make someone stop and argue
    for it out loud.

    IT FIRED ONCE, FOR repo_open_pr, AND HERE IS THE ARGUMENT.
    ---------------------------------------------------------
    The bar this set sets is not "is the action safe" — every executor is
    behind a human key. It is "should the MODEL be able to name this".

    member_depart fails that bar: it executes, but "Comrade suggests you leave
    the team" is not a card this product puts in anyone's thread.

    repo_open_pr passes it, and more cleanly than either task tool. Proposing a
    change and having a human approve it is the entire point of the capability
    — an agent that can edit a working copy but cannot ask for the change to be
    reviewed has done nothing at all. It is team-visible, reversible by closing
    the PR, and the approval is a member reading a diff, which is a better
    review than a consent card usually gets.

    What bounds it is not this set. It is that the executor pushes only to a
    comrade/ branch and never to a default branch, so the worst an approval can
    produce is a pull request somebody then declines to merge.
    """
    from shared.consent import _EXECUTORS, AGENT_PROPOSABLE

    assert "post_group_message" not in _EXECUTORS
    assert AGENT_PROPOSABLE == {"task_create", "task_update", "repo_open_pr"}
    assert "member_depart" not in AGENT_PROPOSABLE
