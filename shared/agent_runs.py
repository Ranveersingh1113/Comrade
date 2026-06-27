"""Durable run/step logging for agent turns (observability + crash recovery).

Each turn opens an agent_runs row, appends one step per tool call / tool result /
text chunk as it happens, then closes the row done|failed. All writes go through
the AGENT role + team_session, so RLS (team_id = current_team()) confines them to
the one team.
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
    return str(row[0])


def append_step(team_id: str, run_id: str, step: dict[str, Any]) -> None:
    """Append one ordered step to the run and bump current_step."""
    with team_session(Role.AGENT, team_id) as conn:
        conn.execute(
            "update public.agent_runs"
            " set steps = steps || %s::jsonb, current_step = current_step + 1"
            " where id = %s",
            (Json([step]), run_id),
        )


def finish_run(team_id: str, run_id: str, status: str) -> None:
    """Close the run with a terminal status ('done' | 'failed')."""
    with team_session(Role.AGENT, team_id) as conn:
        conn.execute(
            "update public.agent_runs set status = %s, finished_at = now()"
            " where id = %s",
            (status, run_id),
        )


def get_run(team_id: str, run_id: str) -> dict[str, Any] | None:
    """Read a run back under the AGENT role (team-scoped). None if not visible."""
    with team_session(Role.AGENT, team_id) as conn:
        row = conn.execute(
            "select id, trigger_type, input_summary, steps, current_step, status,"
            " finished_at from public.agent_runs where id = %s",
            (run_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": str(row[0]),
        "trigger_type": row[1],
        "input_summary": row[2],
        "steps": row[3],
        "current_step": row[4],
        "status": row[5],
        "finished_at": row[6].isoformat() if row[6] else None,
    }
