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
import logging
import uuid
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Json

logger = logging.getLogger(__name__)

from shared.errors import safe_error

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
    # T2 -- shared and reversible. A pull request is visible to the whole team
    # and closable by any of them, which is the definition the tier carries.
    # Not T1: it lands on the team's repository, not on one member. Not
    # anything higher, because T3 was removed (§10) and because for CODE,
    # branch protection is a stronger second key than a tier ever was (§16.2).
    "repo_open_pr": "T2",
}
DEFAULT_TIER = "T2"

# Thread grants deliberately cover only reversible, member-scoped task work.
# A repository publication or departure remains an explicit one-time decision.
_REUSABLE_GRANT_RISK = {
    "task_create": "member",
    "task_update": "member",
}


def _permission_resource(tool_name: str, args: dict) -> dict | None:
    if tool_name == "task_create":
        return {"assignee_id": args.get("assignee_id")}
    if tool_name == "task_update":
        return {"task_id": args.get("task_id")}
    return None


def _grantable(tool_name: str, thread_id: str | None, args: dict) -> tuple[str, dict]:
    risk = _REUSABLE_GRANT_RISK.get(tool_name)
    resource = _permission_resource(tool_name, args)
    if not thread_id or risk is None or resource is None:
        raise ConsentError(f"{tool_name} may only be allowed once")
    # 🔴 CLOSING WORK WAS GRANTABLE. `task_update` is reusable for a thread, and
    # the grant covered any later update — including status='done'. One "allow
    # for this thread" and the agent could close a team's work off the back of
    # its own prose, which is the one task outcome a person has to actually
    # decide. Marking something finished is not a routine edit.
    if tool_name == "task_update" and args.get("status") == "done":
        raise ConsentError(
            "marking work done is a decision a person makes, and is asked"
            " every time"
        )
    return risk, resource


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
    thread_id: str | None = None,
    agent_run_id: str | None = None,
) -> dict:
    """Write a pending consent item (does NOT perform the action). 7-day backstop.

    The id is generated here rather than with RETURNING: RETURNING needs SELECT
    on `consent_queue`, and findings §4.1 left the agent INSERT-only there. A
    freshly proposed row is 'pending' by definition — that is what proposing is.

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
    # 🔴 The same reasoning, one step further. `task_update` may only set four
    # columns — `status` is deliberately not among them, which is what makes
    # "never auto-close work" true — but the check ran at EXECUTE time. So the
    # agent could queue a card asking to close work, a member could approve
    # it, and only then would it fail. Refusing at propose time means the
    # agent is told immediately and nobody is asked to approve something that
    # cannot happen.
    if tool_name == "task_update":
        bad = set(args) - {"task_id", *_TASK_UPDATE_COLUMNS}
        if bad:
            raise ConsentError(
                f"task_update may only set {_TASK_UPDATE_COLUMNS}, got"
                f" {sorted(bad)}"
            )

    action_hash = compute_hash(tool_name, team_id, requester_id, args)
    final_tier = resolve_tier(tool_name, tier)
    consent_id = str(uuid.uuid4())
    try:
        with team_session(Role.AGENT, team_id) as conn:
            conn.execute(
                "insert into public.consent_queue (id, team_id,"
                " requesting_member_id, tool_name, tool_args, source_snippet,"
                " action_hash, reversible, tier, thread_id, agent_run_id, expires_at)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now() + interval '7 days')",
                (consent_id, team_id, requester_id, tool_name, Json(args),
                 source_snippet, action_hash, reversible, final_tier, thread_id,
                 agent_run_id),
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
    grant_id = _approve_with_thread_grant(
        team_id, requester_id, consent_id, tool_name, args, thread_id,
    )
    if grant_id is not None:
        result = execute_consent(team_id, consent_id)
        if result.get("status") == "executed":
            return {**result, "permission_grant_id": grant_id}
    return {
        "consent_id": consent_id,
        "status": "pending",
        "tool_name": tool_name,
        "action_hash": action_hash,
        "tier": final_tier,
    }


def _approve_with_thread_grant(
    team_id: str, requester_id: str, consent_id: str, tool_name: str,
    args: dict, thread_id: str | None,
) -> str | None:
    """Consume a matching active grant and turn this exact action into approval."""
    resource = _permission_resource(tool_name, args)
    risk = _REUSABLE_GRANT_RISK.get(tool_name)
    if not thread_id or resource is None or risk is None:
        return None
    # The same rule on the CONSUMING side. A grant that already exists must
    # not quietly close work either — the check belongs wherever a standing
    # permission is turned into an action, not only where one is created.
    if tool_name == "task_update" and args.get("status") == "done":
        return None
    with user_session(requester_id) as conn:
        row = conn.execute(
            "select id from public.permission_grants where team_id=%s and thread_id=%s"
            " and requesting_member_id=%s and tool_name=%s and resource_constraint=%s::jsonb"
            " and max_risk_class=%s and revoked_at is null and expires_at > now()"
            " for update",
            (team_id, thread_id, requester_id, tool_name, Json(resource), risk),
        ).fetchone()
        if row is None:
            return None
        approved = conn.execute(
            "update public.consent_queue set status='approved'"
            " where id=%s and team_id=%s and status='pending' returning id",
            (consent_id, team_id),
        ).fetchone()
    return str(row[0]) if approved is not None else None


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
            " expires_at, agent_run_id",
            (consent_id,),
        ).fetchone()
        if claimed is None:
            return {"status": "noop", "reason": "not approved or already executed"}

        (tool_name, args, action_hash, requester_id, expires_at, agent_run_id) = claimed

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
        return {"status": "executed", "result": result, "agent_run_id": str(agent_run_id) if agent_run_id else None}


def _requeue_permission_run(team_id: str, run_id: str | None) -> None:
    if not run_id:
        return
    with team_session(Role.AGENT, team_id) as conn:
        conn.execute(
            "update public.agent_runs set status='queued', worker_id=null, lease_expires_at=null"
            " where id=%s and status='waiting_for_permission'",
            (run_id,),
        )


# ---------- human-side resolution (authorization happens here, via RLS) ----------
# The approver acts as themselves; RLS (au_consent_queue_update: requester only)
# is the authorization. execute_consent below never receives the approver id.
#
# 🔴 AND THE TEAM IS NOT RLS'S JOB HERE. Two policies cover this table and
# neither covers both halves:
#
#   au_consent_queue_update   requesting_member_id = auth.uid()   <- no team
#   ex_consent_queue          team_id = current_team()            <- no owner
#
# The requester side is team-blind, so `where id=%s` matched a row in ANY team
# the caller had a proposal in, and the team_id the caller passed then went
# straight to execute_consent to open the executor session. Nothing asked
# whether the row was in that team. server/app.py checks the caller belongs to
# the team they name, which is a different question.
#
# The damage was worst in edit_and_approve, which recomputes the action hash:
# a mismatched team_id stamped a hash bound to the wrong team onto the row, and
# no correct call could ever match it again. The item became permanently
# unexecutable with nothing saying why.
#
# So every requester-side statement below carries team_id explicitly. The row
# either belongs to the named team or is not found.

def _create_thread_grant(
    team_id: str, requester_id: str, consent_id: str, tool_name: str,
    args: dict, thread_id: str | None,
) -> str:
    risk, resource = _grantable(tool_name, thread_id, args)
    with user_session(requester_id) as conn:
        existing = conn.execute(
            "select id from public.permission_grants where team_id=%s and thread_id=%s"
            " and requesting_member_id=%s and tool_name=%s and resource_constraint=%s::jsonb"
            " and revoked_at is null and expires_at > now()",
            (team_id, thread_id, requester_id, tool_name, Json(resource)),
        ).fetchone()
        if existing is not None:
            return str(existing[0])
        row = conn.execute(
            "insert into public.permission_grants (team_id, thread_id,"
            " requesting_member_id, tool_name, resource_constraint, max_risk_class,"
            " source_consent_id, expires_at)"
            " values (%s,%s,%s,%s,%s,%s,%s, now() + interval '24 hours') returning id",
            (team_id, thread_id, requester_id, tool_name, Json(resource), risk, consent_id),
        ).fetchone()
    return str(row[0])


def revoke_permission_grant(team_id: str, grant_id: str, requester_id: str) -> bool:
    """Revoke one requester-owned thread grant. Resolved grants stay revoked."""
    with user_session(requester_id) as conn:
        row = conn.execute(
            "update public.permission_grants set revoked_at=now()"
            " where id=%s and team_id=%s and revoked_at is null returning id",
            (grant_id, team_id),
        ).fetchone()
    return row is not None


def approve_consent(
    team_id: str, consent_id: str, approver_id: str, *, grant_for_thread: bool = False,
) -> dict:
    """Requester approves a pending item; it executes immediately.

    Since §10 removed T3, approval is always the last key. There is no
    waiting state.
    """
    with user_session(approver_id) as conn:
        consent = conn.execute(
            "select tool_name, tool_args, thread_id from public.consent_queue"
            " where id=%s and team_id=%s and status='pending' for update",
            (consent_id, team_id),
        ).fetchone()
        if consent is None:
            return {"status": "not_approved", "reason": "not pending or not yours"}
        tool_name, args, thread_id = consent
        if grant_for_thread:
            _grantable(tool_name, str(thread_id) if thread_id else None, args)
        row = conn.execute(
            "update public.consent_queue set status='approved'"
            " where id=%s and team_id=%s and status='pending' returning id",
            (consent_id, team_id),
        ).fetchone()
    if row is None:
        return {"status": "not_approved", "reason": "not pending or not yours"}
    result = execute_consent(team_id, consent_id)
    if result.get("status") == "executed":
        if grant_for_thread:
            try:
                result["permission_grant_id"] = _create_thread_grant(
                    team_id, approver_id, consent_id, tool_name, args,
                    str(thread_id) if thread_id else None,
                )
            except psycopg.Error as exc:
                # Consent already executed. Do not lie that it did not; report
                # the failed optional grant so caller can retry with Allow once.
                result["permission_grant_error"] = str(exc)
        _requeue_permission_run(team_id, result.get("agent_run_id"))
    return result


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
            " resolution_reason=%s where id=%s and team_id=%s"
            " and status='pending' returning id, agent_run_id",
            (reason, consent_id, team_id),
        ).fetchone()
    if row is None:
        return {"status": "not_found"}
    # 🔴 This was missing. Approve and edit-and-approve both resumed the
    # waiting run; reject wrote its reason and stopped, so the run stayed
    # parked and there was never a next turn to read the reason on. "No" is an
    # answer, and the model has to be running to receive it.
    _requeue_permission_run(team_id, str(row[1]) if row[1] else None)
    return {"status": "rejected"}


def edit_and_approve(
    team_id: str, consent_id: str, approver_id: str, new_args: dict
) -> dict:
    """Requester edits the args (re-stamping the hash) and approves, then it executes."""
    with user_session(approver_id) as conn:
        row = conn.execute(
            "select tool_name, requesting_member_id from public.consent_queue"
            " where id=%s and team_id=%s and status='pending'",
            (consent_id, team_id),
        ).fetchone()
        if row is None:
            return {"status": "not_approved", "reason": "not pending or not yours"}
        tool_name, requester_id = row
        # Safe to bind team_id into the hash now, and only now: the select
        # above proved the row is in that team. Before it did, this line wrote
        # a hash the row could never match again.
        new_hash = compute_hash(tool_name, team_id, str(requester_id), new_args)
        conn.execute(
            "update public.consent_queue set tool_args=%s, action_hash=%s,"
            " status='edited' where id=%s and team_id=%s",
            (Json(new_args), new_hash, consent_id, team_id),
        )
    result = execute_consent(team_id, consent_id)
    if result.get("status") == "executed":
        _requeue_permission_run(team_id, result.get("agent_run_id"))
    return result


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


def _precheck_repo_open_pr(conn, team_id, requester_id, args) -> None:
    if not (args.get("patch") or "").strip():
        raise ConsentError("the proposal carries no change")
    if not (args.get("repo_full_name") or "").strip():
        raise ConsentError("the proposal names no repository")


def _exec_repo_open_pr(conn, team_id, requester_id, args) -> dict:
    """Push the approved change to a branch and open a pull request.

    NETWORK I/O INSIDE THE CAS TRANSACTION, and there is no way around it: the
    thing being executed lives at GitHub. execute_consent claims the row and
    calls this inside one Postgres transaction, so a push that succeeds before
    a rollback leaves a branch the row denies.

    The answer is not a cleverer transaction. It is an action that is safe to
    repeat: pipeline/repo_pr.py derives the branch from the action_hash, starts
    every apply from a clean base, and returns an already-open PR rather than
    opening a second one. A retry converges on the same pull request.

    The patch travels in the consent row rather than being read from disk at
    this moment, because by now `sync_repo` may have reset the checkout several
    times — see repo_pr's header. What a member approved is what applies.
    """
    from pipeline.repo_pr import PullRequestError, open_pull_request

    row = conn.execute(
        "select 1 from public.github_repos where team_id=%s and repo_full_name=%s",
        (team_id, args["repo_full_name"]),
    ).fetchone()
    if row is None:
        raise ConsentError(
            f"{args['repo_full_name']} is no longer connected to this team"
        )

    # Recomputed rather than threaded through: execute_consent has already
    # verified that this exact value matches the stored action_hash, so it is
    # the same string by construction — and the alternative is a fifth
    # parameter on every executor for the sake of one.
    action_hash = compute_hash("repo_open_pr", team_id, requester_id, args)

    try:
        result = open_pull_request(
            team_id=team_id,
            repo_full_name=args["repo_full_name"],
            title=args.get("title") or "Change proposed by Comrade",
            body=args.get("body") or "",
            patch=args["patch"],
            action_hash=action_hash,
        )
    except PullRequestError as exc:
        raise ConsentError(str(exc)) from exc

    # Remember whose work this is. Without it a failing check has no thread to
    # report to, and the delivery becomes repository trivia for the wiki while
    # the people who wrote the change hear nothing.
    from pipeline.ci import record_pull_request
    from pipeline.repo_pr import branch_for

    thread_id = conn.execute(
        "select thread_id from public.consent_queue"
        " where team_id=%s and action_hash=%s order by created_at desc limit 1",
        (team_id, action_hash),
    ).fetchone()
    number = result.get("number")
    if number is None:
        # 🔴 This used to be `int(result["number"])` against an adapter that
        # returned no number at all, so every call raised KeyError into the
        # `except` below and NOTHING was ever correlated. Named rather than
        # swallowed: a missing number means the adapter changed shape, and a
        # silent log is how that goes unnoticed for a release.
        logger.error(
            "the pull request adapter returned no number, so this work cannot"
            " be correlated with its thread: keys=%s", sorted(result)
        )
    else:
        try:
            record_pull_request(
                team_id, args["repo_full_name"], int(number),
                branch_for(action_hash),
                thread_id=str(thread_id[0]) if thread_id and thread_id[0] else None,
                action_hash=action_hash,
                head_sha=result.get("head_sha"),
            )
        except Exception as exc:  # noqa: BLE001 - the PR is open either way
            # Recoverable without opening a second pull request: opening one is
            # idempotent, so a later attempt returns the same PR through the
            # already-exists path and writes the mapping then.
            logger.error("could not record the pull request for %s: %s",
                         team_id, safe_error(exc))
    return result


_PRECHECKS = {
    "task_create": _precheck_task_create,
    "task_update": _precheck_task_update,
    "member_depart": _precheck_member_depart,
    "repo_open_pr": _precheck_repo_open_pr,
}
_EXECUTORS = {
    "task_create": _exec_task_create,
    "task_update": _exec_task_update,
    "member_depart": _exec_member_depart,
    "repo_open_pr": _exec_repo_open_pr,
}

# What the MODEL may name in a proposal, which is not the same set as what can
# be executed. Until member_depart existed the two were identical and nothing
# had to say so: a removed legacy batch tool handed an LLM-chosen tool_name
# straight to propose_action, and the `not in _EXECUTORS` check happened to
# reject everything else. That validation was accidental — a side effect of
# the executor map being two entries long — and adding a third entry would
# have quietly given the agent a new verb.
#
# That tool was removed 2026-09-04, so today no model-chosen name reaches
# here: every proposal tool hardcodes its own action. This set is now the
# belt to that braces, and it stays — held by construction is not the same
# as checked, and the construction is one refactor away from changing.
#
# It must never gain member_depart. Not because the agent could remove anyone
# (it cannot; the key stays with the member) but because "Comrade suggests you
# leave the team" is not a card this product puts in anyone's thread, and
# because the pending-hash index means such a card would block the real one a
# teammate tried to send.
AGENT_PROPOSABLE = frozenset({"task_create", "task_update", "repo_open_pr"})
#
# THE THIRD ENTRY, AND THE ARGUMENT THE COMMENT ABOVE ASKED FOR.
#
# The bar this set sets is not "is the action safe" — every executor is gated
# by a human key. It is "should the MODEL be able to name this". member_depart
# fails that bar: it executes, but "Comrade suggests you leave the team" is not
# a card this product puts in anyone's thread.
#
# repo_open_pr passes it, and passes it more cleanly than either task tool.
# Proposing a change and having a human approve it is the ENTIRE POINT of the
# capability — an agent that can edit a working copy but cannot ask for the
# change to be reviewed has done nothing. It is team-visible, it is reversible
# (close the PR), and the approval is a member reading a diff, which is a
# better review than a consent card usually gets.
#
# What keeps it bounded is not this set. It is that the executor pushes only to
# a comrade/ branch, never to a default branch, so the worst an approval can do
# is create a pull request somebody then declines to merge.
