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
import uuid
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Json

from shared.db import Role, team_session, user_session


class ConsentError(Exception):
    """Raised when an approved action can no longer be safely executed."""


# Blast-radius tiers: T0 read-only, T1 affects one member, T2 shared and
# reversible. T3 (external/irreversible/money) and its two-key requirement
# were removed by owner decision 2026-08-12 (findings §10) — for code, GitHub
# branch protection is a stronger second key than the trigger ever was
# (§16.2). `tier` survives as an informational label and as the seed for the
# earned-trust ratchet (§9.3 G4/G5).
_TIER_ORDER = {"T0": 0, "T1": 1, "T2": 2}

# Per-tool FLOORS — a proposal may raise its own tier, never lower it below
# these. T0-T2 is the whole range now (§10): for code, GitHub branch
# protection is the real backstop (§16.2); for non-code actions there is no
# equivalent floor yet — that gap is real and not covered here.
_TOOL_TIER_FLOORS = {
    "task_create": "T1",           # assignee-confirm is the affected member's key
    "task_update": "T1",           # ditto -- reassignment moves the same key
    # Affects exactly one member -- the one holding the key. See
    # _exec_member_depart for why those are always the same person.
    "member_depart": "T1",
}
DEFAULT_TIER = "T2"


def resolve_tier(tool_name: str, requested: str | None = None) -> str:
    """The proposal's tier: the requested one, floored per tool."""
    floor = _TOOL_TIER_FLOORS.get(tool_name, DEFAULT_TIER)
    assert floor in _TIER_ORDER, f"bad floor {floor!r} registered for {tool_name!r}"
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
    batch_id: str | None = None,
) -> dict:
    """Write a pending consent item (does NOT perform the action). 7-day backstop.

    The id is generated here rather than with RETURNING: RETURNING needs SELECT
    on `consent_queue`, and findings §4.1 left the agent INSERT-only there. A
    freshly proposed row is 'pending' by definition — that is what proposing is.

    batch_id is a display grouping ONLY (task 6): it ties several proposals
    into one card group in the inbox. It must never become an approval gate —
    every row stays individually approvable/rejectable regardless of what it
    shares a batch with. None (the default) is a lone proposal, unchanged from
    before this parameter existed; propose_batch below is what sets it.
    """
    # §13.5: a proposal naming a tool with no executor can never execute, and
    # today that is discovered only when a human approves it — the worst
    # possible moment. Refuse to queue it. This is also what would have turned
    # Phase 0's post_group_message removal into a red suite, not a green one.
    if tool_name not in _EXECUTORS:
        raise ConsentError(
            f"no executor registered for {tool_name} — refusing to queue a"
            " proposal that could never execute"
        )

    action_hash = compute_hash(tool_name, team_id, requester_id, args)
    final_tier = resolve_tier(tool_name, tier)
    consent_id = str(uuid.uuid4())
    try:
        with team_session(Role.AGENT, team_id) as conn:
            conn.execute(
                "insert into public.consent_queue (id, team_id,"
                " requesting_member_id, tool_name, tool_args, source_snippet,"
                " action_hash, reversible, tier, batch_id, expires_at)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now() + interval '7 days')",
                (consent_id, team_id, requester_id, tool_name, Json(args),
                 source_snippet, action_hash, reversible, final_tier, batch_id),
            )
    except psycopg.errors.UniqueViolation:
        # The same proposal is already pending. That is uq_consent_pending_hash
        # doing its job (findings §2.2), not a failure — a retried turn is
        # asking for exactly the idempotency the index exists to provide, and
        # raising here killed the whole turn instead. Hand back the card that
        # already exists.
        #
        # Read as the requester, not as the agent: §4.1 left comrade_agent
        # INSERT-only on consent_queue, and au_consent_queue_select lets a
        # member see their own items. Safe because action_hash binds the
        # requester — a duplicate hash is always this requester's own row.
        with user_session(requester_id) as conn:
            row = conn.execute(
                "select id, tier from public.consent_queue"
                " where team_id=%s and action_hash=%s and status='pending'",
                (team_id, action_hash),
            ).fetchone()
        if row is None:
            raise  # resolved between the insert and the read — not idempotency
        consent_id, final_tier = str(row[0]), row[1]
    return {
        "consent_id": consent_id,
        "status": "pending",
        "tool_name": tool_name,
        "action_hash": action_hash,
        "tier": final_tier,
    }


def propose_batch(team_id: str, requester_id: str, items: list[dict]) -> dict:
    """Propose several actions as one card GROUP, not one approval.

    findings §5 / task 6: multi-step work produced five separate,
    unrelated-looking consent cards with no way to see they belonged together.
    This writes one generated batch_id across every row so the inbox can
    display them under one heading — nothing more. It is NOT a gate: each row
    stays individually approvable and individually rejectable, same as if
    propose_action had been called on it directly. Approving four of five
    must work and must leave the fifth pending; rejecting one must never
    touch its siblings. Do not add a batch-level approve/reject on top of
    this — that would let one contentious item ride through bundled with four
    obvious ones, which is exactly the consent-fatigue failure batching exists
    to avoid, not to enable.

    items: one dict per proposal, each shaped like propose_action's own
    keyword arguments minus team_id/requester_id (which are shared by the
    whole batch) — {"tool_name": str, "args": dict, "source_snippet": str |
    None, "reversible": bool, "tier": str | None}. Only tool_name and args are
    required per item.

    Partial failure is BEST-EFFORT, not atomic: each item is proposed
    independently, so one item naming a tool with no executor fails only that
    item (status "failed", with an "error" message, no consent_id) while the
    rest of the batch still queues and still shares the batch_id. Rationale:
    (1) the only synchronous validation propose_action performs is the
    tool_name check, so a real cross-item transaction would buy atomicity
    against just that one failure mode and nothing else — not worth the
    complexity for a partial guarantee; (2) comrade_agent has INSERT/UPDATE
    but no DELETE grant on consent_queue (findings §4.1's propose-only role),
    so a compensating rollback of already-inserted rows is not even available
    without a privilege change; (3) most importantly, discarding four valid,
    unrelated-to-the-bug proposals because a fifth was malformed would punish
    the member for the agent's mistake and reintroduce all-or-nothing thinking
    at the write layer — the same failure shape this task forbids at the
    approval layer, just moved one step earlier.
    """
    if not items:
        raise ValueError("propose_batch needs at least one item")
    batch_id = str(uuid.uuid4())
    results = []
    for item in items:
        try:
            results.append(
                propose_action(
                    team_id=team_id,
                    requester_id=requester_id,
                    tool_name=item["tool_name"],
                    args=item["args"],
                    source_snippet=item.get("source_snippet"),
                    reversible=item.get("reversible", True),
                    tier=item.get("tier"),
                    batch_id=batch_id,
                )
            )
        except ConsentError as e:
            results.append(
                {"status": "failed", "tool_name": item.get("tool_name"), "error": str(e)}
            )
    return {"batch_id": batch_id, "items": results}


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

        (tool_name, args, action_hash, requester_id, expires_at) = claimed

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
    """Requester approves a pending item; it executes immediately.

    Since §10 removed T3, approval is always the last key. There is no
    waiting state.
    """
    with user_session(approver_id) as conn:
        row = conn.execute(
            "update public.consent_queue set status='approved'"
            " where id=%s and status='pending' returning id",
            (consent_id,),
        ).fetchone()
    if row is None:
        return {"status": "not_approved", "reason": "not pending or not yours"}
    return execute_consent(team_id, consent_id)


def reject_consent(
    team_id: str, consent_id: str, approver_id: str, reason: str | None = None
) -> dict:
    """Requester rejects a pending item (it will never execute).

    `reason` is written to resolution_reason so the agent can read WHY on its
    next turn (§9.3 G3) instead of just THAT, and stop re-proposing it blind.
    """
    with user_session(approver_id) as conn:
        row = conn.execute(
            "update public.consent_queue set status='rejected', resolved_at=now(),"
            " resolution_reason=%s where id=%s and status='pending' returning id",
            (reason, consent_id),
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


def _precheck_task_update(conn, team_id, requester_id, args) -> None:
    ok = conn.execute(
        "select 1 from public.tasks where id=%s and team_id=%s",
        (args.get("task_id"), team_id),
    ).fetchone()
    if ok is None:
        raise ConsentError("task no longer exists in this team")
    if "assignee_id" in args:
        ok = conn.execute(
            "select 1 from public.memberships where team_id=%s and user_id=%s"
            " and status='active'",
            (team_id, args["assignee_id"]),
        ).fetchone()
        if ok is None:
            raise ConsentError("assignee is no longer an active team member")


# The ONLY columns an AI-proposed update may ever touch. Never status, never
# confirmed_at: trg_tasks_confirm_guard (20260612101500_triggers.sql) lets only
# the assignee themselves move a task out of 'proposed' or set confirmed_at,
# and it checks auth.uid() -- which is NULL for comrade_executor. That is
# correct and must not be worked around here.
_TASK_UPDATE_COLUMNS = ("title", "description", "deadline", "assignee_id")


def _exec_task_update(conn, team_id, requester_id, args) -> dict:
    # Refuse the WHOLE proposal if it reaches for a column outside the
    # permitted four (in particular status/confirmed_at) -- silently applying
    # the rest and dropping only the disallowed key would mean the card a
    # human approved shows something different from what actually happened.
    bad = set(args) - {"task_id", *_TASK_UPDATE_COLUMNS}
    if bad:
        raise ConsentError(
            f"task_update may only set {_TASK_UPDATE_COLUMNS}, got {sorted(bad)}"
        )
    fields = {col: args[col] for col in _TASK_UPDATE_COLUMNS if col in args}
    if not fields:
        raise ConsentError("task_update proposal changes nothing")
    set_sql = ", ".join(f"{col}=%s" for col in fields)
    row = conn.execute(
        f"update public.tasks set {set_sql} where id=%s and team_id=%s returning id",
        (*fields.values(), args["task_id"], team_id),
    ).fetchone()
    if row is None:
        raise ConsentError("task no longer exists in this team")
    return {"task_id": str(row[0])}


def _precheck_member_depart(conn, team_id, requester_id, args) -> None:
    if str(args.get("user_id")) != str(requester_id):
        raise ConsentError("a departure may only be approved by the member leaving")
    ok = conn.execute(
        "select 1 from public.memberships where team_id=%s and user_id=%s"
        " and status='active'",
        (team_id, requester_id),
    ).fetchone()
    if ok is None:
        raise ConsentError("already not an active member of this team")


def _exec_member_depart(conn, team_id, requester_id, args) -> dict:
    """A member leaves, because they said so — asked by someone, decided by them.

    The whole point of routing this through consent rather than giving anyone a
    remove button: §23.1 says nobody configures another member's participation.
    So a teammate may ASK (server/app.py member_departure_request), and the
    request lands in the affected member's own inbox as a card only they can
    see and only they can approve.

    Which is why the identity check below is the load-bearing line, not a
    formality. `requesting_member_id` is the key-holder — RLS
    (au_consent_queue_update) lets only that member approve. If args named
    SOMEONE ELSE, an asker could file a proposal against a teammate, approve
    it with their own key, and the consent queue would have become the admin
    removal power this design exists to refuse. Belt and braces: the precheck
    rejects the mismatch, and this uses requesting_member_id regardless of
    what args say, so a divergence can never execute against the wrong person.
    """
    if str(args.get("user_id")) != str(requester_id):
        raise ConsentError("a departure may only be approved by the member leaving")
    row = conn.execute(
        "update public.memberships set status='left', left_at=now()"
        " where team_id=%s and user_id=%s and status='active' returning id",
        (team_id, requester_id),
    ).fetchone()
    if row is None:
        raise ConsentError("already not an active member of this team")
    return {"user_id": str(requester_id)}


_PRECHECKS = {
    "task_create": _precheck_task_create,
    "task_update": _precheck_task_update,
    "member_depart": _precheck_member_depart,
}
_EXECUTORS = {
    "task_create": _exec_task_create,
    "task_update": _exec_task_update,
    "member_depart": _exec_member_depart,
}

# What the MODEL may name in a proposal, which is not the same set as what can
# be executed. Until member_depart existed the two were identical and nothing
# had to say so: agent/tools.py hands team_propose_batch's LLM-chosen
# tool_name straight to propose_action, and the `not in _EXECUTORS` check
# happened to reject everything else. That validation was accidental — a side
# effect of the executor map being two entries long — and adding a third entry
# would have quietly given the agent a new verb.
#
# It must never gain member_depart. Not because the agent could remove anyone
# (it cannot; the key stays with the member) but because "Comrade suggests you
# leave the team" is not a card this product puts in anyone's inbox, and
# because the pending-hash index means such a card would block the real one a
# teammate tried to send.
AGENT_PROPOSABLE = frozenset({"task_create", "task_update"})
