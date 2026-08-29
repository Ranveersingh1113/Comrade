"""The agent reads as the member who invoked it, never as itself.

findings §2.1: ag_messages_select was team-scoped, not thread-scoped, so the
agent role could select every member's private thread. §4.1's fix is to delete
the agent's read grants rather than add policies — reads run under the
requester's own RLS.
"""
import uuid

import psycopg
import pytest

from agent.tools import fetch_team_state, read_memory_page
from shared.config import settings
from shared.db import Role, team_session
from tests._seed import A1, A2, B1, B2, TEAM_A, TEAM_B, as_user, count


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _seed_team_b_work(cur):
    cur.execute(
        "insert into public.tasks (team_id, assignee_id, title, status,"
        " created_by_kind, created_by_id) values"
        " (%s,%s,'B-only secret task','proposed','user',%s)",
        (TEAM_B, B2, B1),
    )


def test_agent_role_can_no_longer_read_messages(seeded):
    """The grant is gone, so the leak is structurally impossible."""
    with pytest.raises(psycopg.Error):
        with team_session(Role.AGENT, TEAM_A) as conn:
            conn.execute("select body from public.messages").fetchall()


def test_agent_role_can_still_insert_its_own_reply(seeded):
    """Writes stay on Role.AGENT — only reads moved (§4.1).

    No RETURNING: that clause needs SELECT on the table, which is exactly what
    this task revokes, so the writers supply the id themselves.
    """
    message_id = str(uuid.uuid4())
    with team_session(Role.AGENT, TEAM_A) as conn:
        conn.execute(
            "insert into public.messages (id, team_id, thread_type,"
            " thread_owner_id, sender_kind, body)"
            " values (%s,%s,'group',null,'ai','still works')",
            (message_id, TEAM_A),
        )
    conn = _admin()
    try:
        assert count(
            conn, "select count(*) from public.messages where id = %s", (message_id,)
        ) == 1
    finally:
        conn.close()


def test_team_state_reads_as_the_requester(seeded):
    state = fetch_team_state(TEAM_A, A1)
    assert state["team"]["id"] == TEAM_A
    assert {m["user_id"] for m in state["members"]} == {A1, A2}


def test_memory_page_reads_as_the_requester(seeded):
    """A member can read the team wiki; the projection is unchanged."""
    result = read_memory_page(TEAM_A, A1, "Uncategorized")
    assert "error" not in result or result["error"] == "no such page"


def test_team_state_never_reaches_a_team_the_requester_is_not_in(seeded):
    """A1 belongs to TEAM_A only, so TEAM_B is invisible to them outright."""
    conn = _admin()
    try:
        with conn.cursor() as cur:
            _seed_team_b_work(cur)
    finally:
        conn.close()

    state = fetch_team_state(TEAM_A, A1)
    assert {m["user_id"] for m in state["members"]} == {A1, A2}
    assert all(t["title"] != "B-only secret task" for t in state["tasks"])


def test_team_state_excludes_the_requesters_other_team(seeded):
    """The test that catches a missing team_id filter.

    Under `authenticated` there is no current_team() to scope by: A1, now a
    member of both teams, may legitimately SELECT TEAM_B's rows. Only the
    explicit `where team_id = %s` on every query keeps TEAM_B out of TEAM_A's
    snapshot.
    """
    conn = _admin()
    try:
        with conn.cursor() as cur:
            _seed_team_b_work(cur)
            cur.execute(
                "insert into public.memberships (team_id, user_id, role, status)"
                " values (%s,%s,'member','active')",
                (TEAM_B, A1),
            )
    finally:
        conn.close()

    # the premise: A1 really can see TEAM_B now, so the assertions below are
    # about the query's filter and not about RLS doing the work for us.
    with as_user(A1) as conn:
        assert count(
            conn, "select count(*) from public.tasks where team_id = %s", (TEAM_B,)
        ) == 1

    state = fetch_team_state(TEAM_A, A1)
    assert state["team"]["id"] == TEAM_A
    assert {m["user_id"] for m in state["members"]} == {A1, A2}
    assert {B1, B2}.isdisjoint({m["user_id"] for m in state["members"]})
    assert all(t["title"] != "B-only secret task" for t in state["tasks"])
