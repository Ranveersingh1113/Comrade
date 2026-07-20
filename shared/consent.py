"""Consent mechanism for group-visible (gated) actions.

The agent only ever PROPOSES (propose_action -> a pending consent_queue row). A
human approves it elsewhere. execute_consent then performs the real write under
the separate comrade_executor role. The approver's identity is deliberately not
part of this mechanism — execute only checks status.

The action_hash binds {tool, server-bound team, server-bound requester, args}
into one token at propose time and re-verifies it at execute time, so the
executed action is exactly the approved one (an edit is the only legitimate
mutation and must re-stamp the hash). It is NOT anti-tamper against a DB-write
attacker — that is what the role split + RLS provide.
"""
import hashlib
import json
from datetime import datetime, timezone

from psycopg.types.json import Json

from shared.db import Role, team_session, user_session


class ConsentError(Exception):
    """Raised when an approved action can no longer be safely executed."""


# Blast-radius tiers (governance ruling, provisional): T0 read-only, T1
# affects one member, T2 shared+reversible, T3 external/irreversible/money.
_TIER_ORDER = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}

# Per-tool FLOORS — a proposal may raise its own tier, never lower it below
# these. Money / outbound-to-non-members tools must be registered at T3.
_TOOL_TIER_FLOORS = {
    "task_create": "T1",           # assignee-confirm is the affected member's key
    "post_group_message": "T2",    # shared, visible, reversible (delete trace)
}
DEFAULT_TIER = "T2"


def resolve_tier(tool_name: str, requested: str | None = None) -> str:
    """The proposal's tier: the requested one, floored per tool."""
    floor = _TOOL_TIER_FLOORS.get(tool_name, DEFAULT_TIER)
    if requested is None or requested not in _TIER_ORDER:
        return floor
    return requested if _TIER_ORDER[requested] >= _TIER_ORDER[floor] else floor


def compute_hash(tool_name: str, team_id: str, requester_id: str, args: dict) -> str:
    payload = json.dumps(
        {
            "tool": tool_name,
            "team": str(team_id),
            "requester": str(requester_id),
            "args": args,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def propose_action(
    team_id: str,
    requester_id: str,
    tool_name: str,
    args: dict,
    source_snippet: str | None = None,
    reversible: bool = True,
    tier: str | None = None,
) -> dict:
    """Write a pending consent item (does NOT perform the action). 7-day backstop."""
    action_hash = compute_hash(tool_name, team_id, requester_id, args)
    final_tier = resolve_tier(tool_name, tier)
    with team_session(Role.AGENT, team_id) as conn:
        row = conn.execute(
            "insert into public.consent_queue (team_id, requesting_member_id,"
            " tool_name, tool_args, source_snippet, action_hash, reversible,"
            " tier, expires_at)"
            " values (%s,%s,%s,%s,%s,%s,%s,%s, now() + interval '7 days')"
            " returning id, status",
            (team_id, requester_id, tool_name, Json(args), source_snippet,
             action_hash, reversible, final_tier),
        ).fetchone()
    return {
        "consent_id": str(row[0]),
        "status": row[1],
        "tool_name": tool_name,
        "action_hash": action_hash,
        "tier": final_tier,
    }


def execute_consent(team_id: str, consent_id: str) -> dict:
    """Execute an approved/edited consent item exactly once (CAS).

    A second call is a no-op. Hash/expiry/precondition failures roll back the
    claim (status returns to approved) and raise ConsentError.
    """
    with team_session(Role.EXECUTOR, team_id) as conn:
        # CAS claim: only an approved/edited row transitions to executed.
        claimed = conn.execute(
            "update public.consent_queue set status='executed', resolved_at=now()"
            " where id=%s and status in ('approved','edited')"
            " returning tool_name, tool_args, action_hash, requesting_member_id,"
            " expires_at, tier, second_approver_id",
            (consent_id,),
        ).fetchone()
        if claimed is None:
            return {"status": "noop", "reason": "not approved or already executed"}

        (tool_name, args, action_hash, requester_id, expires_at,
         tier, second_approver_id) = claimed

        if expires_at is not None and expires_at < datetime.now(timezone.utc):
            raise ConsentError(f"consent {consent_id} has expired")

        # T3 hard gate: two keys, and the second is never the initiator (the
        # DB check constraint backs this; re-checked here so the error is a
        # clean ConsentError rollback, not a constraint failure).
        if tier == "T3" and second_approver_id is None:
            raise ConsentError(
                f"consent {consent_id} is T3 and needs a second key"
                " from another member"
            )

        if compute_hash(tool_name, team_id, requester_id, args) != action_hash:
            raise ConsentError(f"consent {consent_id} hash mismatch")

        precheck = _PRECHECKS.get(tool_name)
        if precheck is not None:
            precheck(conn, team_id, requester_id, args)  # raises ConsentError if stale

        executor = _EXECUTORS.get(tool_name)
        if executor is None:
            raise ConsentError(f"no executor registered for {tool_name}")

        result = executor(conn, team_id, requester_id, args)
        return {"status": "executed", "result": result}


# ---------- human-side resolution (authorization happens here, via RLS) ----------
# The approver acts as themselves; RLS (au_consent_queue_update: requester only)
# is the authorization. execute_consent below never receives the approver id.

def approve_consent(team_id: str, consent_id: str, approver_id: str) -> dict:
    """Requester approves a pending item; it executes once its keys are in.

    For T0–T2 approval IS the last key. A T3 item without its countersign
    stays approved-but-waiting rather than erroring — the second key's
    arrival (add_second_key) completes it.
    """
    with user_session(approver_id) as conn:
        row = conn.execute(
            "update public.consent_queue set status='approved'"
            " where id=%s and status='pending'"
            " returning tier, second_approver_id",
            (consent_id,),
        ).fetchone()
    if row is None:
        return {"status": "not_approved", "reason": "not pending or not yours"}
    tier, second_approver_id = row
    if tier == "T3" and second_approver_id is None:
        return {"status": "approved", "awaiting": "second_key"}
    return execute_consent(team_id, consent_id)


def add_second_key(team_id: str, consent_id: str, member_id: str) -> dict:
    """A teammate countersigns a T3 item; executes if already approved.

    Authorization is RLS + the second-key trigger: the write only succeeds if
    the actor is a team member other than the requester, touches nothing but
    the second-key columns, and stamps themselves.
    """
    with user_session(member_id) as conn:
        row = conn.execute(
            "update public.consent_queue set second_approver_id=%s"
            " where id=%s and tier='T3' and status in ('pending','approved')"
            " and requesting_member_id <> %s"    # initiator is never a key twice
            " returning status",
            (member_id, consent_id, member_id),
        ).fetchone()
    if row is None:
        return {"status": "not_found",
                "reason": "not a countersignable T3 item in your team"}
    if row[0] in ("approved", "edited"):
        return execute_consent(team_id, consent_id)
    return {"status": "countersigned", "awaiting": "requester_approval"}


def reject_consent(team_id: str, consent_id: str, approver_id: str) -> dict:
    """Requester rejects a pending item (it will never execute)."""
    with user_session(approver_id) as conn:
        row = conn.execute(
            "update public.consent_queue set status='rejected', resolved_at=now()"
            " where id=%s and status='pending' returning id",
            (consent_id,),
        ).fetchone()
    return {"status": "rejected" if row is not None else "not_found"}


def edit_and_approve(
    team_id: str, consent_id: str, approver_id: str, new_args: dict
) -> dict:
    """Requester edits the args (re-stamping the hash) and approves, then it executes."""
    with user_session(approver_id) as conn:
        row = conn.execute(
            "select tool_name, requesting_member_id from public.consent_queue"
            " where id=%s and status='pending'",
            (consent_id,),
        ).fetchone()
        if row is None:
            return {"status": "not_approved", "reason": "not pending or not yours"}
        tool_name, requester_id = row
        new_hash = compute_hash(tool_name, team_id, str(requester_id), new_args)
        conn.execute(
            "update public.consent_queue set tool_args=%s, action_hash=%s,"
            " status='edited' where id=%s",
            (Json(new_args), new_hash, consent_id),
        )
    return execute_consent(team_id, consent_id)


# ---------- per-tool executors + precondition checks ----------

def _precheck_task_create(conn, team_id, requester_id, args) -> None:
    ok = conn.execute(
        "select 1 from public.memberships where team_id=%s and user_id=%s"
        " and status='active'",
        (team_id, args.get("assignee_id")),
    ).fetchone()
    if ok is None:
        raise ConsentError("assignee is no longer an active team member")


def _exec_task_create(conn, team_id, requester_id, args) -> dict:
    row = conn.execute(
        "insert into public.tasks (team_id, assignee_id, title, description,"
        " deadline, created_by_kind, created_by_id)"
        " values (%s,%s,%s,%s,%s,'ai',%s) returning id",
        (team_id, args.get("assignee_id"), args["title"], args.get("description"),
         args.get("deadline"), requester_id),
    ).fetchone()
    return {"task_id": str(row[0])}


def _precheck_post_group_message(conn, team_id, requester_id, args) -> None:
    ok = conn.execute(
        "select 1 from public.memberships where team_id=%s and user_id=%s"
        " and status='active'",
        (team_id, requester_id),
    ).fetchone()
    if ok is None:
        raise ConsentError("requester is no longer an active team member")


def _exec_post_group_message(conn, team_id, requester_id, args) -> dict:
    # AI posts to the group as itself (not attributed to the requester)
    row = conn.execute(
        "insert into public.messages (team_id, thread_type, sender_kind, body)"
        " values (%s,'group','ai',%s) returning id",
        (team_id, args["body"]),
    ).fetchone()
    return {"message_id": str(row[0])}


_PRECHECKS = {
    "task_create": _precheck_task_create,
    "post_group_message": _precheck_post_group_message,
}
_EXECUTORS = {
    "task_create": _exec_task_create,
    "post_group_message": _exec_post_group_message,
}
