"""Declarations that only work if something else was also declared.

Every bug this file guards has the same shape: two places have to agree, one of
them was updated, and nothing failed until a user hit the path. They are
invisible to unit tests because a unit test exercises one side.

  * A row-level policy is DEAD without the matching table grant. Postgres
    checks privilege BEFORE it consults RLS, so `for all to comrade_pipeline`
    on a table the role holds no UPDATE on refuses the write with a permission
    error, while the policy that would have allowed it reads as correct. Hit
    twice on this branch: github_repos.last_cloned_at and the executor's read
    of github_repos.
  * A tool the model may PROPOSE with no executor is a consent card a member
    can approve and nothing happens.

The job-handler version of this lives in test_worker_handlers.py, which needs a
subprocess; these can be answered from the schema.
"""
import psycopg
import pytest

from shared.config import settings

#: Which table privilege each policy command actually needs. A policy for a
#: command the role cannot perform is unreachable.
_NEEDS = {
    "SELECT": ("SELECT",),
    "INSERT": ("INSERT",),
    "UPDATE": ("UPDATE",),
    "DELETE": ("DELETE",),
    # ALL is satisfied by any one of them: a role with only SELECT and an ALL
    # policy is reading under that policy, which is legitimate.
    "ALL": ("SELECT", "INSERT", "UPDATE", "DELETE"),
}

#: The roles this codebase actually connects as. `public` and `authenticated`
#: are covered too — au_* policies are the browser's API surface.
_OUR_ROLES = {
    "comrade_agent", "comrade_executor", "comrade_pipeline", "comrade_control",
    "authenticated",
}


@pytest.fixture(scope="module")
def db():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def test_every_policy_has_the_grant_it_needs(db):
    """🔴 A policy without a grant is a permission error with a correct-looking
    policy sitting next to it.

    Postgres checks table privilege first, so the policy never runs and the
    error names the grant — which is the last place anyone reading a `for all
    to comrade_pipeline` line thinks to look. Both times this happened here,
    the fix was one GRANT and the diagnosis took far longer than the fix.
    """
    rows = db.execute(
        "select schemaname, tablename, policyname, cmd, roles"
        "  from pg_policies where schemaname = 'public'"
    ).fetchall()
    assert rows, "no policies found; is the schema applied?"

    dead = []
    for schema, table, policy, cmd, roles in rows:
        for role in roles:
            if role not in _OUR_ROLES:
                continue
            needed = _NEEDS[cmd]
            has_any = any(
                    db.execute(
                        "select has_table_privilege(%s, %s, %s)"
                        " or has_any_column_privilege(%s, %s, %s)",
                        (role, f"{schema}.{table}", priv,
                         role, f"{schema}.{table}", priv),
                ).fetchone()[0]
                for priv in needed
            )
            if not has_any:
                dead.append(
                    f"{policy} on {table}: {role} has no"
                    f" {'/'.join(needed)} privilege, so the policy never runs"
                )
    assert not dead, "policies that cannot fire:\n  " + "\n  ".join(sorted(dead))


def test_every_tool_the_model_may_propose_can_actually_be_executed(db):
    """🔴 A proposal with no executor is a card a member approves into silence.

    AGENT_PROPOSABLE is what the MODEL may name. _EXECUTORS is what
    execute_consent can run. They are deliberately different sets — plenty of
    things execute that the agent may not propose — but every proposable tool
    must be in both, or approving it does nothing and reports nothing.
    """
    from shared.consent import AGENT_PROPOSABLE, _EXECUTORS

    missing = sorted(AGENT_PROPOSABLE - set(_EXECUTORS))
    assert not missing, (
        f"the agent may propose {missing}, and approving one would find no"
        " executor. A member sees the card, approves it, and nothing happens."
    )


def test_every_proposable_tool_declares_a_consent_tier(db):
    """A tool with no tier floor falls back to the default, which means a
    change to the default silently reclassifies it — the opposite of a
    protocol whose whole promise is that the tier is explicit."""
    from shared.consent import AGENT_PROPOSABLE, _TOOL_TIER_FLOORS

    missing = sorted(AGENT_PROPOSABLE - set(_TOOL_TIER_FLOORS))
    assert not missing, (
        f"proposable tools with no declared tier floor: {missing}"
    )


def test_every_agent_tool_is_declared_in_the_registry():
    """The chokepoint refuses a tool it has no ToolSpec for, so an undeclared
    tool is dead on arrival — but only at runtime, on the turn that reaches
    for it."""
    from agent.agent import root_agent
    from agent.registry import UNKNOWN, spec_for

    undeclared = sorted(
        t.__name__ for t in root_agent.tools if spec_for(t.__name__) is UNKNOWN
    )
    assert not undeclared, (
        f"tools wired into the agent with no ToolSpec: {undeclared}."
        " The chokepoint refuses these at call time."
    )
