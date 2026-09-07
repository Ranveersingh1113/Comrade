"""Crash-safe result cache for write-capable agent tool calls."""
import hashlib
import json
from typing import Any

from psycopg.types.json import Json

from shared.db import Role, team_session


class EffectUncertain(RuntimeError):
    """The worker died while an effect may have been executing."""


class RunInactive(RuntimeError):
    """The run was cancelled or otherwise stopped before the next effect."""


def run_is_active(
    team_id: str, run_id: str, *, worker_id: str | None = None,
) -> bool:
    """Is this run still the one THIS worker should be spending money on?

    Two questions in one, because a turn must stop for either answer.

    Is it still running — cancellation had exactly one observer before,
    `claim_effect`, which refused a WRITE on a stopped run. Read-only tools
    kept running and the model kept being called, so "stop" meant "stop
    eventually": the turn carried on to its natural end, billing a team for
    work nobody wanted.

    And is it still OURS — a worker whose lease expired had its run handed to
    somebody else and carried on regardless. `renew_lease` returning False
    ended the renewer thread and told nothing else. Passing `worker_id` makes
    the loss observable at the same boundary as a cancellation.
    """
    owned = "" if worker_id is None else " and worker_id is not distinct from %s"
    params: tuple = (run_id,) if worker_id is None else (run_id, worker_id)
    with team_session(Role.AGENT, team_id) as conn:
        return conn.execute(
            "select 1 from public.agent_runs where id=%s and status='running'"
            + owned,
            params,
        ).fetchone() is not None


def _key(tool: str, args: dict[str, Any]) -> str:
    value = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{tool}:{value}".encode()).hexdigest()


def claim_effect(
    team_id: str, run_id: str, tool: str, args: dict[str, Any],
    *, worker_id: str | None,
) -> dict | None:
    """Claim an effect, or return its completed result on a resumed run.

    🔴 `worker_id` is the fence, and it was missing. The claim tested only that
    the run was `running`, which says nothing about WHO is running it: a worker
    whose lease expired and whose run was handed to somebody else found the
    status true again — for the other worker — and performed the effect anyway.
    The same external action, done twice, by two processes each believing they
    owned the job.

    `is not distinct from` rather than `=`, so a run nobody leased (worker_id
    null, an inline turn) matches a null caller and nothing else. That keeps
    it a fence instead of a hole.
    """
    key = _key(tool, args)
    owns = (
        "select 1 from public.agent_runs where id=%s and status='running'"
        " and worker_id is not distinct from %s"
    )
    with team_session(Role.AGENT, team_id) as conn:
        inserted = conn.execute(
            "insert into public.agent_effects (run_id, team_id, effect_key, tool, args)"
            " select %s,%s,%s,%s,%s where exists (" + owns + ")"
            " on conflict (run_id,effect_key) do nothing"
            " returning id",
            (run_id, team_id, key, tool, Json(args), run_id, worker_id),
        ).fetchone()
        if inserted is not None:
            return None
        row = conn.execute(
            "select status, result from public.agent_effects where run_id=%s and effect_key=%s",
            (run_id, key),
        ).fetchone()
    if row is None:
        with team_session(Role.AGENT, team_id) as conn:
            active = conn.execute(owns, (run_id, worker_id)).fetchone()
        if active is None:
            raise RunInactive(
                "this run is no longer this worker's to act on — it was"
                " cancelled, or its lease expired and somebody else has it"
            )
        raise LookupError("effect claim disappeared")
    if row[0] == "completed":
        return row[1]
    raise EffectUncertain(f"{tool} may have completed before the worker stopped")


def complete_effect(
    team_id: str, run_id: str, tool: str, args: dict[str, Any], result: dict,
) -> None:
    key = _key(tool, args)
    with team_session(Role.AGENT, team_id) as conn:
        cur = conn.execute(
            "update public.agent_effects set status='completed', result=%s, completed_at=now()"
            " where run_id=%s and effect_key=%s and status='started'",
            (Json(result), run_id, key),
        )
    if cur.rowcount != 1:
        raise LookupError("effect was not claimed by this run")


def completed_effects(team_id: str, run_id: str) -> list[dict]:
    """The compact, durable handoff record for a reclaimed run."""
    with team_session(Role.AGENT, team_id) as conn:
        rows = conn.execute(
            "select tool, result from public.agent_effects"
            " where run_id=%s and status='completed' order by completed_at, id",
            (run_id,),
        ).fetchall()
    return [{"tool": tool, "result": result} for tool, result in rows]
