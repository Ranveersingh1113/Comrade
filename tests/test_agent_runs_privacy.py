"""agent_runs must not leak one member's private turn to their teammates.

Findings §2.1 closed the private-thread leak on `messages`. It missed the
second copy: `agent_runs.input_summary` stores the member's prompt verbatim
(`agent/runtime.py` passes `user_text[:200]`) and `steps` stores every tool
result. `au_agent_runs_select` was `is_team_member(team_id)`, so any teammate
could read any member's private turn straight from the browser.

Worse than §2.1, which was latent: this one was live and browser-reachable.

Nothing member-facing reads the table — no frontend reference exists — so the
fix deletes the grant rather than narrowing it. The hourly turn budget, the
only reader that ran as `authenticated`, moves to the agent role, which is
team-scoped by `current_team()` and never hands rows to a member.
"""
import json

import psycopg
import pytest

from shared.agent_runs import append_step, start_run
from shared.config import settings
from tests._seed import A1, A2, TEAM_A


def _as(uid):
    conn = psycopg.connect(
        settings.comrade_authenticator_db_url or settings.comrade_db_url_admin
    )
    conn.execute("set role authenticated")
    conn.execute(
        "select set_config('request.jwt.claims', %s, false)",
        (json.dumps({"sub": uid, "role": "authenticated"}),),
    )
    return conn


def test_a_teammate_cannot_read_another_members_agent_run(seeded):
    run_id = start_run(TEAM_A, "user", "A1 private: my appointment is Tuesday")
    append_step(
        TEAM_A, run_id,
        {"seq": 0, "type": "tool_result", "tool": "x",
         "response": {"secret": "A1 private thread content"}},
    )
    conn = _as(A2)
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "select input_summary, steps from public.agent_runs where team_id=%s",
                (TEAM_A,),
            ).fetchall()
    finally:
        conn.close()


def test_a_member_cannot_read_even_their_own_agent_runs(seeded):
    """Deletion, not narrowing: nothing member-facing reads this table.

    If a "your run history" surface is ever built, that is the moment to add a
    requester column and an own-rows-only policy — not before.
    """
    start_run(TEAM_A, "user", "A1 asked something")
    conn = _as(A1)
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("select id from public.agent_runs").fetchall()
    finally:
        conn.close()


def test_the_executor_cannot_read_messages(seeded):
    """The §2.1 shape on a sibling role.

    `grant select on messages to comrade_executor` was team-scoped and
    thread-scoped by nothing. It was load-bearing only for the group-message
    executor's `returning id`, which §13 removed — so it is dead privilege of
    exactly the kind the role split exists to prevent.
    """
    from shared.db import Role, team_session

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with team_session(Role.EXECUTOR, TEAM_A) as conn:
            conn.execute("select body from public.messages").fetchall()
