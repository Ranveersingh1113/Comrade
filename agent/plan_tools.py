"""A thread's plan: optional, versioned, visible to whoever can read the thread.

Not a proposal. The plan changes one row inside the thread and produces no
effect anyone outside it can see, so it carries no consent card — the registry
declares it needs_human=False for the same reason a repo_edit does not stop for
approval per file.

The version is the load-bearing part. Steering (Task 10) means a second
participant's message can land mid-run, and two runs writing a step list with
no version would each overwrite the other with whatever they last read. A
stale write is refused and handed the live plan instead of guessing.

thread_id and team_id are bound from session state, never from a model
argument: the tool's parameters name STEPS and a version, and nothing else.
"""
from typing import Any, Literal

from google.adk.tools import ToolContext
from psycopg.types.json import Json
from pydantic import BaseModel, ValidationError

from shared.db import Role, team_session

# The insert and the compare-and-swap in one statement: `expected_version=0`
# means "there is no plan yet", so a first write onto an existing plan hits the
# same WHERE and is refused rather than silently replacing it.
_UPSERT = (
    "insert into public.thread_plans (thread_id, team_id, steps)"
    " values (%s,%s,%s)"
    " on conflict (thread_id) do update set"
    "   version = thread_plans.version + 1,"
    "   steps = excluded.steps,"
    "   updated_at = now()"
    " where thread_plans.version = %s"
    " returning version, steps"
)


class PlanStep(BaseModel):
    id: str
    text: str
    status: Literal["pending", "active", "completed", "blocked"] = "pending"


def _checked(steps: list[Any]) -> list[dict]:
    """The step list as rows, or ValueError naming what is wrong with it."""
    parsed = [PlanStep.model_validate(step).model_dump() for step in steps]
    ids = [step["id"] for step in parsed]
    if len(set(ids)) != len(ids):
        raise ValueError("every step needs its own id")
    # One thing at a time. A plan with two active steps is a plan that has
    # stopped saying where the work is.
    if sum(step["status"] == "active" for step in parsed) > 1:
        raise ValueError("only one step may be active at a time")
    return parsed


def read_plan(team_id: str, thread_id: str) -> dict | None:
    """This thread's plan, or None if it never made one."""
    with team_session(Role.AGENT, team_id) as conn:
        row = conn.execute(
            "select version, steps from public.thread_plans where thread_id=%s",
            (thread_id,),
        ).fetchone()
    return None if row is None else {"version": row[0], "steps": row[1]}


def update_plan(
    team_id: str, thread_id: str, steps: list[Any], expected_version: int = 0,
) -> dict:
    """Write the thread's plan. Pure of ADK; unit-tested directly.

    Returns a top-level `status` the model cannot skim past — ok, conflict or
    invalid — because a per-step detail with no verdict is how a refused write
    gets reported as a completed one.
    """
    try:
        rows = _checked(steps)
    except (ValidationError, ValueError) as exc:
        return {"status": "invalid", "detail": str(exc)}
    with team_session(Role.AGENT, team_id) as conn:
        written = conn.execute(
            _UPSERT, (thread_id, team_id, Json(rows), expected_version),
        ).fetchone()
        if written is not None:
            return {"status": "ok", "version": written[0], "steps": written[1]}
        live = conn.execute(
            "select version, steps from public.thread_plans where thread_id=%s",
            (thread_id,),
        ).fetchone()
    return {
        "status": "conflict",
        "version": live[0],
        "steps": live[1],
        "detail": (
            f"the plan is at version {live[0]}, not {expected_version}. This is"
            " the current plan — re-apply your change on top of it."
        ),
    }


def plan_update(
    steps: list[PlanStep], tool_context: ToolContext, expected_version: int = 0,
) -> dict:
    """Record or revise the plan for this piece of work. OPTIONAL — most work
    needs no plan at all.

    Use it for work with dependent steps, work that will run for a while, work
    several people are following, or when someone asks you to plan. Do NOT use
    it for a question, a search, a quick check, or an obvious small edit:
    a three-step plan for a one-step change is noise in the thread.

    Send the WHOLE step list every time, not just what changed. Keep at most
    one step `active` — the one you are doing now.

    Args:
        steps: the full plan. Each step is {id, text, status}, where status is
            pending, active, completed or blocked.
        expected_version: the version you are amending, from the last plan
            result. Omit it (0) only when writing the first plan for this
            thread. If it comes back `conflict`, someone changed the plan since
            you read it — the reply carries the current one; re-apply on top.
    """
    return update_plan(
        tool_context.state["team_id"],
        tool_context.state["thread_id"],
        steps,
        expected_version,
    )
