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
) -> dict:
    """Write a pending consent item (does NOT perform the action). 7-day backstop."""
    action_hash = compute_hash(tool_name, team_id, requester_id, args)
    with team_session(Role.AGENT, team_id) as conn:
        row = conn.execute(
            "insert into public.consent_queue (team_id, requesting_member_id,"
            " tool_name, tool_args, source_snippet, action_hash, reversible,"
            " expires_at) values (%s,%s,%s,%s,%s,%s,%s, now() + interval '7 days')"
            " returning id, status",
            (team_id, requester_id, tool_name, Json(args), source_snippet,
             action_hash, reversible),
        ).fetchone()
    return {
        "consent_id": str(row[0]),
        "status": row[1],
        "tool_name": tool_name,
        "action_hash": action_hash,
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
            " expires_at",
            (consent_id,),
        ).fetchone()
        if claimed is None:
            return {"status": "noop", "reason": "not approved or already executed"}

        tool_name, args, action_hash, requester_id, expires_at = claimed

        if expires_at is not None and expires_at < datetime.now(timezone.utc):
            raise ConsentError(f"consent {consent_id} has expired")

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
    """Requester approves a pending item, then it executes."""
    with user_session(approver_id) as conn:
        row = conn.execute(
            "update public.consent_queue set status='approved'"
            " where id=%s and status='pending' returning id",
            (consent_id,),
        ).fetchone()
    if row is None:
        return {"status": "not_approved", "reason": "not pending or not yours"}
    return execute_consent(team_id, consent_id)


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


_PRECHECKS = {"task_create": _precheck_task_create}
_EXECUTORS = {"task_create": _exec_task_create}
