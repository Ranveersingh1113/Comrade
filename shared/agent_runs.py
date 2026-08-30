"""Durable run/step logging for agent turns (observability + crash recovery).

Each turn opens an agent_runs row, appends one step per tool call / tool result /
text chunk as it happens, then closes the row done|failed. All writes go through
the AGENT role + team_session, so RLS (team_id = current_team()) confines them to
the one team.

Steps live in their own table, agent_steps (one row per step, seq-ordered), not
in agent_runs.steps jsonb: `steps || x` rewrote the whole array on every append,
so an N-step turn wrote O(N^2) bytes. agent_runs.steps and .current_step stay in
the schema (dropping a column is a harder migration to reverse than adding one)
but nothing here writes them anymore; a later phase drops both once nothing
reads them. See supabase/migrations/20260830120000_agent_steps.sql.
"""
from typing import Any

from psycopg.types.json import Json

from shared.db import Role, team_session


def start_run(team_id: str, trigger_type: str, input_summary: str) -> str:
    """Open a running agent_runs row. Returns the run id."""
    with team_session(Role.AGENT, team_id) as conn:
        row = conn.execute(
            "insert into public.agent_runs (team_id, trigger_type, input_summary)"
            " values (%s, %s, %s) returning id",
            (team_id, trigger_type, input_summary),
        ).fetchone()
    if row is None:
        raise RuntimeError(f"agent_runs insert returned no row for team {team_id!r}")
    return str(row[0])


def append_step(team_id: str, run_id: str, step: dict[str, Any]) -> None:
    """Insert one step row (seq comes from the caller, see agent/runtime.py's
    _steps_from_event). One INSERT per step instead of rewriting a jsonb array:
    an N-step turn now writes O(N) bytes total, not O(N^2).

    The insert's source is gated by an EXISTS check on agent_runs, which RLS
    confines to current_team() -- a run_id that doesn't exist, or belongs to
    another team, both insert zero rows, exactly like the old UPDATE's
    rowcount check did.
    """
    args = step.get("args")
    response = step.get("response")
    with team_session(Role.AGENT, team_id) as conn:
        cur = conn.execute(
            "insert into public.agent_steps"
            " (run_id, team_id, seq, type, tool, args, response, text)"
            " select %(run_id)s, %(team_id)s, %(seq)s, %(type)s, %(tool)s,"
            "        %(args)s, %(response)s, %(text)s"
            " where exists (select 1 from public.agent_runs where id = %(run_id)s)",
            {
                "run_id": run_id,
                "team_id": team_id,
                "seq": step["seq"],
                "type": step["type"],
                "tool": step.get("tool"),
                "args": Json(args) if args is not None else None,
                "response": Json(response) if response is not None else None,
                "text": step.get("text"),
            },
        )
        if cur.rowcount == 0:
            raise LookupError(
                f"agent_run {run_id!r} not found or not accessible for team {team_id!r}"
            )


def finish_run(team_id: str, run_id: str, status: str) -> None:
    """Close the run with a terminal status ('done' | 'failed')."""
    with team_session(Role.AGENT, team_id) as conn:
        cur = conn.execute(
            "update public.agent_runs set status = %s, finished_at = now()"
            " where id = %s",
            (status, run_id),
        )
        if cur.rowcount == 0:
            raise LookupError(
                f"agent_run {run_id!r} not found or not accessible for team {team_id!r}"
            )


def _step_from_row(row: tuple) -> dict[str, Any]:
    """Rebuild one step dict from an agent_steps row. Only the columns the
    caller actually populated come back as keys, matching the shape the old
    jsonb blob held (e.g. a text step has no "tool" key at all)."""
    seq, type_, tool, args, response, text = row
    step: dict[str, Any] = {"seq": seq, "type": type_}
    if tool is not None:
        step["tool"] = tool
    if args is not None:
        step["args"] = args
    if response is not None:
        step["response"] = response
    if text is not None:
        step["text"] = text
    return step


def get_run(team_id: str, run_id: str) -> dict[str, Any] | None:
    """Read a run back under the AGENT role (team-scoped). None if not visible."""
    with team_session(Role.AGENT, team_id) as conn:
        row = conn.execute(
            "select id, trigger_type, input_summary, status, finished_at"
            " from public.agent_runs where id = %s",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        step_rows = conn.execute(
            "select seq, type, tool, args, response, text"
            " from public.agent_steps where run_id = %s order by seq",
            (run_id,),
        ).fetchall()
    steps = [_step_from_row(r) for r in step_rows]
    return {
        "id": str(row[0]),
        "trigger_type": row[1],
        "input_summary": row[2],
        "steps": steps,
        "current_step": len(steps),
        "status": row[3],
        "finished_at": row[4].isoformat() if row[4] else None,
    }
