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


def start_run(
    team_id: str,
    requester_id: str,
    thread_id: str,
    input_message_id: str | None,
    trigger_type: str,
    input_summary: str,
) -> str:
    """Open a running agent_runs row. Returns the run id."""
    with team_session(Role.AGENT, team_id) as conn:
        row = conn.execute(
            "insert into public.agent_runs"
            " (team_id, requester_id, thread_id, input_message_id, trigger_type, input_summary)"
            " values (%s, %s, %s, %s, %s, %s) returning id",
            (team_id, requester_id, thread_id, input_message_id, trigger_type, input_summary),
        ).fetchone()
    if row is None:
        raise RuntimeError(f"agent_runs insert returned no row for team {team_id!r}")
    return str(row[0])


def append_step(
    team_id: str, run_id: str, step: dict[str, Any], *, worker_id: str | None = None
) -> None:
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
            " where exists (select 1 from public.agent_runs where id = %(run_id)s"
            # Cast for the same reason as finish_run below: a bare
            # `%(worker_id)s is null` has no column beside it to take a type
            # from. The second occurrence sits next to worker_id and would be
            # fine on its own, which is what makes this easy to miss.
            " and (%(worker_id)s::text is null or (worker_id = %(worker_id)s"
            " and status = 'running' and lease_expires_at >= now())))",
            {
                "run_id": run_id,
                "team_id": team_id,
                "seq": step["seq"],
                "type": step["type"],
                "tool": step.get("tool"),
                "args": Json(args) if args is not None else None,
                "response": Json(response) if response is not None else None,
                "text": step.get("text"),
                "worker_id": worker_id,
            },
        )
        if cur.rowcount == 0:
            raise LookupError(
                f"agent_run {run_id!r} not found or not accessible for team {team_id!r}"
            )


def _cost_usd(input_tokens: int, output_tokens: int) -> float | None:
    """What those tokens cost, or None when nobody has said.

    Deliberately None rather than 0.0 when the rates are unset: zero is a
    price, and a column full of zeros reads as "this was free" rather than
    "nobody configured this". The token counts beside it are unconditional,
    so the question stays answerable either way.
    """
    from shared.config import settings

    rate_in = settings.gemini_input_usd_per_mtok
    rate_out = settings.gemini_output_usd_per_mtok
    if not rate_in and not rate_out:
        return None
    return round(
        (input_tokens * rate_in + output_tokens * rate_out) / 1_000_000, 6
    )


def finish_run(
    team_id: str,
    run_id: str,
    status: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    worker_id: str | None = None,
) -> None:
    """Close the run with a terminal status ('done' | 'failed') and what it cost.

    The token columns have existed on agent_runs since the table did and were
    never written, so every question about what the agent actually spends —
    which teams, which kinds of turn, whether the empty-turn retry matters —
    had no data behind it. They are written on the FAILED path too: a turn that
    failed still consumed the prompt it was handed, and accounting that only
    counts successes understates exactly the runs worth investigating.
    """
    with team_session(Role.AGENT, team_id) as conn:
        cur = conn.execute(
            "update public.agent_runs set status = %s, finished_at = now(),"
            " input_tokens = %s, output_tokens = %s, cost_usd = %s"
            # 🔴 The cast is load-bearing. A bare `%s is null` gives Postgres
            # nothing to infer the parameter's type from — no column, no
            # operator with a known operand — so it answers
            # "could not determine data type of parameter $6" and the whole
            # turn fails. Every other placeholder here sits beside a column and
            # types itself; this one is the exception because its only job is
            # to ask whether a worker id was supplied at all.
            " where id = %s and (%s::text is null"
            "                    or (worker_id = %s and status = 'running'))",
            (status, input_tokens, output_tokens,
             _cost_usd(input_tokens, output_tokens), run_id, worker_id, worker_id),
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
            "select id, requester_id, thread_id, input_message_id, trigger_type,"
            " input_summary, status, finished_at"
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
        "requester_id": str(row[1]) if row[1] else None,
        "thread_id": str(row[2]) if row[2] else None,
        "input_message_id": str(row[3]) if row[3] else None,
        "trigger_type": row[4],
        "input_summary": row[5],
        "steps": steps,
        "current_step": len(steps),
        "status": row[6],
        "finished_at": row[7].isoformat() if row[7] else None,
    }
