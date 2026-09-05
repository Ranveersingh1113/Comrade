"""Crash-safe result cache for write-capable agent tool calls."""
import hashlib
import json
from typing import Any

from psycopg.types.json import Json

from shared.db import Role, team_session


class EffectUncertain(RuntimeError):
    """The worker died while an effect may have been executing."""


def _key(tool: str, args: dict[str, Any]) -> str:
    value = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{tool}:{value}".encode()).hexdigest()


def claim_effect(team_id: str, run_id: str, tool: str, args: dict[str, Any]) -> dict | None:
    """Claim an effect, or return its completed result on a resumed run."""
    key = _key(tool, args)
    with team_session(Role.AGENT, team_id) as conn:
        inserted = conn.execute(
            "insert into public.agent_effects (run_id, team_id, effect_key, tool, args)"
            " values (%s,%s,%s,%s,%s) on conflict (run_id,effect_key) do nothing"
            " returning id",
            (run_id, team_id, key, tool, Json(args)),
        ).fetchone()
        if inserted is not None:
            return None
        row = conn.execute(
            "select status, result from public.agent_effects where run_id=%s and effect_key=%s",
            (run_id, key),
        ).fetchone()
    if row is None:
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
