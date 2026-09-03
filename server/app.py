"""HTTP entrypoint for the Comrade agent.

Most of the product talks to Postgres directly from the browser under RLS. This
service exists only for the things RLS cannot express:

  * running an agent turn (needs the Gemini key and the agent's DB role),
  * executing an approved consent item (needs the separate executor role),
  * enqueuing a document job (members deliberately cannot write `jobs`).

Identity always comes from the verified Supabase JWT (see server/auth.py); the
model never receives team_id / requester_id as tool arguments.
"""
import base64
import json
import logging
import uuid

from fastapi import (
    Depends, FastAPI, File, HTTPException, Request, UploadFile, status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from agent.runtime import run_turn_sync, stream_turn
from pipeline.compiler import enqueue_document
from pipeline.chat import enqueue_remember
from pipeline.repo_sync import enqueue_sync
from pipeline.github import (
    enqueue_github_event, forget_installation,
    resolve_team_for_installation, resolve_team_for_repo,
)
from server.github_connect import (
    ConnectError, connectable_repositories, install_url,
    record_installation,
)
from server.auth import CurrentUserId, require_membership
from server.invites import invite_member
from server.webhooks import verify_signature
from shared.config import settings
from shared.consent import (
    ConsentError, approve_consent, edit_and_approve, propose_action,
    reject_consent,
)
from shared.db import Role, connect, team_session, user_session

logger = logging.getLogger(__name__)

app = FastAPI(title="Comrade Agent Runtime")

# The frontend is a browser SPA on a different origin. Credentials are carried
# in the Authorization header (not cookies), so allow_credentials stays off.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

# Binary kinds are shipped to the worker base64-encoded; text kinds go inline.
_BINARY_KINDS = {"pdf", "docx"}


class TeamScoped(BaseModel):
    team_id: str


class TurnRequest(TeamScoped):
    text: str
    thread_type: str = Field(default="private", pattern="^(private|group)$")


class TurnResponse(BaseModel):
    run_id: str
    reply: str
    user_message_id: str
    reply_message_id: str | None = None


class EditApproveRequest(TeamScoped):
    args: dict


class RejectRequest(TeamScoped):
    reason: str | None = None


@app.get("/health")
def health(response: Response) -> dict[str, str]:
    """Alive AND able to reach the database.

    🔴 This returned {"status": "ok"} unconditionally, without touching
    anything. So an instance whose connection pool held dead handles — every
    request failing with `could not receive data from server` — reported
    itself healthy. A deploy check passes, a load balancer keeps routing to
    it, an orchestrator never restarts it, and the only symptom is that
    nothing works.

    Found when a browser journey failed to execute an approved consent item
    while /health said 200. It is the same shape as the rest of this
    codebase's worst bugs: a signal reporting success for something it never
    checked.

    THE TRADE, stated because it is a real one: coupling liveness to a
    dependency means a database blip can restart healthy app instances. At
    this scale that is the better failure — an API that cannot reach Postgres
    can serve no endpoint here except this one, so reporting it alive is a
    lie with no upside. A deployment that autoscales on liveness should split
    this into /health and /ready before relying on it.
    """
    try:
        with connect(Role.ADMIN) as conn:
            conn.execute("select 1")
    except Exception as exc:  # noqa: BLE001 - any failure to reach it counts
        logger.warning("health check could not reach the database: %s", exc)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "database": "unreachable"}
    return {"status": "ok", "database": "ok"}


# ---------- agent ----------

def _persist_user_message(
    user_id: str, team_id: str, thread_type: str, text: str
) -> str:
    """Insert the member's message as themselves, so RLS authorises the write."""
    owner = None if thread_type == "group" else user_id
    with user_session(user_id) as conn:
        row = conn.execute(
            "insert into public.messages (team_id, thread_type, thread_owner_id,"
            " sender_kind, sender_id, body) values (%s,%s,%s,'user',%s,%s)"
            " returning id",
            (team_id, thread_type, owner, user_id, text),
        ).fetchone()
    return str(row[0])


def _persist_ai_reply(
    team_id: str, thread_type: str, owner_id: str | None, text: str
) -> str:
    """Insert the reply as the AI itself — never attributed to the requester.

    The id is generated here rather than with RETURNING: RETURNING needs SELECT
    on `messages`, and findings §4.1 revoked the agent's read of that table.
    """
    message_id = str(uuid.uuid4())
    with team_session(Role.AGENT, team_id) as conn:
        conn.execute(
            "insert into public.messages (id, team_id, thread_type,"
            " thread_owner_id, sender_kind, body) values (%s,%s,%s,%s,'ai',%s)",
            (message_id, team_id, thread_type, owner_id, text),
        )
    return message_id


def _check_turn_budget(team_id: str) -> None:
    """Refuse the turn when the team is at its hourly cap.

    agent_runs already records every turn (team_id, created_at), so the limit
    is a count over a table that exists: no new store, correct across API
    instances, and it survives a restart. idx_agent_runs_team covers the query.

    Runs as the AGENT role, not as the member. Members have no read on
    agent_runs at all — the table holds every private-thread prompt verbatim in
    `input_summary` and every tool result in `steps`, and a team-scoped policy
    over it leaked one member's private turn to their teammates
    (20260830090000_close_agent_runs_leak.sql). The agent role is team-scoped
    by current_team(), and this returns a COUNT to the server, never rows to a
    member — so the cap stays a team cap without reopening the read.
    """
    cap = settings.agent_turns_per_hour
    if cap <= 0:
        return
    with team_session(Role.AGENT, team_id) as conn:
        used = conn.execute(
            "select count(*) from public.agent_runs"
            " where team_id=%s and created_at > now() - interval '1 hour'",
            (team_id,),
        ).fetchone()[0]
    if used >= cap:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"This team has used its {cap} agent turns for the hour."
            " Comrade will be available again shortly.",
        )


@app.post("/agent/turn", response_model=TurnResponse)
def agent_turn(req: TurnRequest, user_id: CurrentUserId) -> TurnResponse:
    """Run one agent turn and persist both sides of it to `messages`.

    A direct reply to an explicit invocation is not consent-gated: the member
    asked, and the answer is visible to exactly the people who could already see
    the question. Actions the agent wants to take on top of that still go
    through propose_action -> the consent queue.
    """
    require_membership(user_id, req.team_id)
    _check_turn_budget(req.team_id)
    user_message_id = _persist_user_message(
        user_id, req.team_id, req.thread_type, req.text
    )
    result = run_turn_sync(
        req.team_id, user_id, req.text,
        thread_type=req.thread_type, exclude_message_id=user_message_id,
    )
    if result.get("busy"):
        # §4.3 + decision Q6: another member's turn holds this room. Say so
        # plainly — a 200 with an empty reply would read as the agent
        # ignoring them, which is worse than being told to wait.
        raise HTTPException(status.HTTP_409_CONFLICT, result["busy"])
    reply = result["reply"]
    reply_message_id = None
    if reply:
        owner = None if req.thread_type == "group" else user_id
        reply_message_id = _persist_ai_reply(
            req.team_id, req.thread_type, owner, reply
        )
    return TurnResponse(
        run_id=result["run_id"],
        reply=reply,
        user_message_id=user_message_id,
        reply_message_id=reply_message_id,
    )


@app.post("/agent/turn/stream")
async def agent_turn_stream(req: TurnRequest, user_id: CurrentUserId):
    """Same turn as /agent/turn, delivered as newline-delimited JSON.

    NDJSON over fetch rather than SSE: EventSource cannot send an
    Authorization header, and a token in the query string would leak into
    logs. One JSON object per line, no frame parsing.

    Both guards run BEFORE the response starts, so a non-member still gets a
    real 403 and an over-budget team a real 429 — never a 200 whose first
    frame is an apology.
    """
    require_membership(user_id, req.team_id)
    _check_turn_budget(req.team_id)
    owner = None if req.thread_type == "group" else user_id
    user_message_id = await run_in_threadpool(
        _persist_user_message, user_id, req.team_id, req.thread_type, req.text
    )

    async def frames():
        reply = ""
        try:
            async for item in stream_turn(
                req.team_id, user_id, req.text,
                thread_type=req.thread_type, exclude_message_id=user_message_id,
            ):
                if item.get("type") == "final":
                    reply = item["reply"]
                    continue
                yield json.dumps(item) + "\n"
        except Exception as exc:  # noqa: BLE001 - the stream owns its errors
            logger.exception("streamed turn failed")
            yield json.dumps({"type": "error", "detail": str(exc)}) + "\n"
            return
        reply_message_id = None
        if reply:
            reply_message_id = await run_in_threadpool(
                _persist_ai_reply, req.team_id, req.thread_type, owner, reply
            )
        yield json.dumps({
            "type": "done",
            "user_message_id": user_message_id,
            "reply_message_id": reply_message_id,
        }) + "\n"

    return StreamingResponse(frames(), media_type="application/x-ndjson")


# ---------- consent ----------
# Authorisation lives in RLS: au_consent_queue_update lets only the requesting
# member resolve their own item, and these helpers act as that user. The API
# adds no permission logic of its own.

def _consent_result(outcome: dict) -> dict:
    if outcome.get("status") in {"not_approved", "not_found"}:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            outcome.get("reason", "consent item not found or not yours"),
        )
    return outcome


@app.post("/consent/{consent_id}/approve")
def consent_approve(
    consent_id: str, req: TeamScoped, user_id: CurrentUserId
) -> dict:
    require_membership(user_id, req.team_id)
    try:
        return _consent_result(approve_consent(req.team_id, consent_id, user_id))
    except ConsentError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@app.post("/consent/{consent_id}/reject")
def consent_reject(
    consent_id: str, req: RejectRequest, user_id: CurrentUserId
) -> dict:
    require_membership(user_id, req.team_id)
    return _consent_result(
        reject_consent(req.team_id, consent_id, user_id, req.reason)
    )


@app.post("/consent/{consent_id}/edit_and_approve")
def consent_edit_and_approve(
    consent_id: str, req: EditApproveRequest, user_id: CurrentUserId
) -> dict:
    require_membership(user_id, req.team_id)
    try:
        return _consent_result(
            edit_and_approve(req.team_id, consent_id, user_id, req.args)
        )
    except ConsentError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


# ---------- GitHub ----------
#
# Only what the BROWSER CANNOT DO ITSELF is here. Connecting and disconnecting
# a repository are row writes the frontend makes straight to Supabase, where
# RLS decides them — au_github_repos_insert requires both team leadership and
# an installation the same team owns, so a route in front of it would add a
# second place to get the same rule right. These three exist because they need
# a credential the browser must never hold.

class InstallationRequest(BaseModel):
    installation_id: int
    state: str
    code: str


@app.get("/teams/{team_id}/github/install")
def github_install(team_id: str, user_id: CurrentUserId) -> dict:
    """Where to send someone to install the App, or why we cannot."""
    require_membership(user_id, team_id)
    return install_url(team_id, user_id)


@app.post("/github/installations")
def github_record_installation(
    req: InstallationRequest, user_id: CurrentUserId
) -> dict:
    """Finish an install.

    NO TEAM IN THE PATH, and that is forced by GitHub rather than chosen: an
    App has ONE fixed Callback URL, so the redirect cannot carry a team in its
    path and a route that required one could never be reached by the flow it
    exists to serve. The team comes out of the signed state token, which is
    also the only trustworthy place for it — a path parameter is whatever the
    caller typed.

    So there is no require_membership call here either. The state token binds
    team AND user, record_installation refuses a session that is not the
    account that started the install, and the insert runs as that user under
    RLS. Three checks, none of which a path parameter could have improved.
    """
    try:
        return record_installation(
            installation_id=req.installation_id, state=req.state,
            code=req.code, session_user_id=user_id,
        )
    except ConnectError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@app.get("/teams/{team_id}/github/repositories")
def github_repositories(team_id: str, user_id: CurrentUserId) -> dict:
    """What this team's installations can reach — the picker's contents."""
    require_membership(user_id, team_id)
    try:
        return connectable_repositories(team_id, user_id)
    except ConnectError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


# ---------- teams ----------

class InviteRequest(BaseModel):
    email: str


@app.post("/teams/{team_id}/invite")
def team_invite(
    team_id: str, req: InviteRequest, user_id: CurrentUserId
) -> dict:
    """Leader invites by email — works for people with no account yet.

    GoTrue sends the invite email and creates the auth user; the membership
    row is written as the leader under their own RLS policy.
    """
    require_membership(user_id, team_id)
    return invite_member(team_id, user_id, req.email)


# Everything the product reads, in the order a person would want to read it
# back. The scoping is not here — it is RLS, running as the caller. That is the
# whole reason this endpoint is short: "the team's data, as far as you can see
# it" is not a filter anyone has to write, it is what a SELECT already means
# for role `authenticated`. Your own private thread comes out; a teammate's
# does not; and tests/test_product_read_paths.py is what keeps that true.
_EXPORT_RELATIONS = (
    ("team", "select * from public.teams where id = %(team)s"),
    ("members",
     "select m.*, p.display_name, p.email from public.memberships m"
     " join public.profiles p on p.id = m.user_id where m.team_id = %(team)s"),
    ("messages",
     "select * from public.messages where team_id = %(team)s order by created_at"),
    ("tasks", "select * from public.tasks where team_id = %(team)s order by created_at"),
    ("milestones",
     "select * from public.milestones where team_id = %(team)s order by due_at"),
    ("documents",
     "select * from public.documents where team_id = %(team)s order by created_at"),
    ("wiki_pages",
     "select * from public.memory_pages where team_id = %(team)s order by title"),
    ("wiki_facts",
     "select v.* from public.memory_versions v where v.team_id = %(team)s"
     " order by v.created_at"),
    ("wiki_citations",
     "select c.* from public.memory_citations c"
     " join public.memory_versions v on v.id = c.version_id"
     " where v.team_id = %(team)s"),
    ("change_log",
     "select * from public.change_log where team_id = %(team)s order by created_at"),
    ("github_repos", "select * from public.github_repos where team_id = %(team)s"),
    ("github_activity",
     "select * from public.github_activity where team_id = %(team)s"
     " order by occurred_at"),
    ("consent_queue",
     "select * from public.consent_queue where team_id = %(team)s order by created_at"),
)


@app.get("/teams/{team_id}/export")
def team_export(team_id: str, user_id: CurrentUserId) -> Response:
    """Take your team's history with you, as one JSON file.

    §23.3 promises memory and history are retained rather than held hostage,
    and this is the promise being keepable: it is also the honest answer to
    "what happens if we stop paying" and the thing to do before you leave —
    once you have left, RLS returns nothing and there is nothing to export.

    ponytail: builds the whole document in memory and returns it in one
    response. Fine at pilot size (a team's entire history is megabytes of
    text); if a team ever outgrows that, stream it relation by relation rather
    than adding pagination to a file nobody reads incrementally.
    """
    require_membership(user_id, team_id)
    out: dict[str, object] = {
        "exported_by": user_id,
        "team_id": team_id,
        "note": (
            "Everything this member could read at export time. A teammate's"
            " private thread is not here, because it was never theirs to read."
        ),
    }
    with user_session(user_id) as conn:
        for name, sql in _EXPORT_RELATIONS:
            cur = conn.execute(sql, {"team": team_id})
            cols = [c.name for c in cur.description or []]
            out[name] = [dict(zip(cols, row)) for row in cur.fetchall()]

    body = json.dumps(out, default=str, indent=2, ensure_ascii=False)
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="comrade-{team_id}.json"'
        },
    )


class DepartureRequest(BaseModel):
    # A note to a person, shown on their card. Capped because it lands in a
    # fixed-size card, not because anything downstream parses it.
    reason: str | None = Field(default=None, max_length=280)


@app.post("/teams/{team_id}/members/{member_id}/departure-request")
def member_departure_request(
    team_id: str, member_id: str, req: DepartureRequest, user_id: CurrentUserId
) -> dict:
    """Ask a teammate to leave. Only they can answer.

    There is no remove-member endpoint and there is not going to be one. §23.1:
    "nobody approves another member's actions, nobody configures permissions."
    So this files a consent proposal whose requesting_member_id is the person
    being asked — which, through au_consent_queue_update, makes their key the
    only one that resolves it. The asker cannot approve their own request
    because RLS will not show them the row.

    Deliberately available to every active member, not just the leader. A
    leader-only version would be the removal power wearing a politer name.
    """
    require_membership(user_id, team_id)
    if member_id == user_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "to leave a team, leave it — this is for asking someone else",
        )

    # Read as the caller, so RLS confirms both people really share this team.
    with user_session(user_id) as conn:
        row = conn.execute(
            "select p.display_name from public.memberships m"
            " join public.profiles p on p.id = m.user_id"
            " where m.team_id=%s and m.user_id=%s and m.status='active'",
            (team_id, member_id),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "not an active member of this team"
            )
        asker = conn.execute(
            "select display_name from public.profiles where id=%s", (user_id,)
        ).fetchone()

    who = (asker[0] if asker else None) or "A teammate"
    note = f"{who} asked you to leave this team."
    if req.reason:
        note = f"{note} They said: {req.reason}"

    return propose_action(
        team_id=team_id,
        requester_id=member_id,
        tool_name="member_depart",
        args={"user_id": member_id},
        source_snippet=note,
        # Not reversible BY THE PERSON DECIDING, which is whose card this is.
        # A leader can invite them back afterwards; that is a new decision by
        # someone else, not an undo they hold.
        reversible=False,
        tier="T1",
    )


@app.post("/teams/{team_id}/messages/{message_id}/remember")
def remember_message(team_id: str, message_id: str, user_id: CurrentUserId) -> dict:
    """Tell Comrade to remember something that was said (§20.7.1).

    Every fact otherwise arrives through automatic extraction, and a member who
    watches the compiler miss something important has no recourse. This is that
    recourse — and the only available mitigation for extractor starvation
    (§20.3.2), since stage 1 is otherwise the sole path from source to memory.

    It does not write a fact. Members cannot write memory_* at all (§6.0), and
    should not: a direct write would be an unspotlighted path straight into
    agent context. It queues a compile of this one message, which then earns a
    citation, a diff card and a revert like anything else.

    The message must be a live GROUP message in this team. §6.0's boundary is
    that private threads never reach memory, and a member must not be able to
    route their own private thread into the shared wiki by tapping a button —
    that is the invariant the whole memory design rests on.
    """
    require_membership(user_id, team_id)
    with user_session(user_id) as conn:
        row = conn.execute(
            "select 1 from public.messages where id=%s and team_id=%s"
            " and thread_type='group' and deleted_scope is null",
            (message_id, team_id),
        ).fetchone()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "no such message in this team's room — private and deleted"
            " messages cannot be remembered",
        )
    return {"job_id": enqueue_remember(team_id, message_id)}


# ---------- observations ----------

# The categories a proactive AI observation can belong to. §12.3: nothing
# produces these yet, so the set is the contract a future producer must match
# — an unvalidated free-text kind records a preference nothing will ever read.
SUPPRESSIBLE_KINDS = frozenset({"proactive_observation"})


class SuppressRequest(TeamScoped):
    kind: str

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, v: str) -> str:
        if v not in SUPPRESSIBLE_KINDS:
            raise ValueError(
                f"unknown observation kind {v!r};"
                f" expected one of {sorted(SUPPRESSIBLE_KINDS)}"
            )
        return v


@app.post("/observations/{message_id}/suppress")
def observation_suppress(
    message_id: str, req: SuppressRequest, user_id: CurrentUserId
) -> dict:
    """One tap: remove a proactive AI observation + record 'don't do this again'.

    The suppression row is written as the member (RLS authorises); the message
    tombstone needs the agent role, because members can only edit their OWN
    messages and AI messages have no sender. Tombstoning, not deleting — the
    deletion-leaves-a-trace invariant applies to the AI too.
    """
    require_membership(user_id, req.team_id)
    with user_session(user_id) as conn:
        row = conn.execute(
            "insert into public.observation_suppressions"
            " (team_id, member_id, kind, message_id)"
            " select %s, %s, %s, m.id from public.messages m"
            " where m.id=%s and m.team_id=%s and m.sender_kind='ai'"
            " and m.thread_type='group'"
            # Diff cards are notifications, not observations: silencing them
            # would break the transparency that replaces a memory approval gate.
            " and not exists (select 1 from public.memory_compilations c"
            "                 where c.diff_message_id = m.id)"
            " returning id",
            (req.team_id, user_id, req.kind, message_id, req.team_id),
        ).fetchone()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "not a suppressible AI observation in this team",
        )
    # The agent role has no UPDATE on messages (propose-only, by design);
    # tombstoning goes through a definer function that can do exactly this
    # one thing to exactly AI messages.
    with team_session(Role.AGENT, req.team_id) as conn:
        conn.execute(
            "select public.tombstone_ai_message(%s, %s)",
            (message_id, req.team_id),
        )
    return {"suppression_id": str(row[0]), "kind": req.kind}


# ---------- webhooks ----------
# THE FIRST UNAUTHENTICATED ROUTE IN THIS CODEBASE (findings §16.6).
#
# Every other endpoint sits behind a verified Supabase JWT. GitHub will not
# present one; its only credential is an HMAC-SHA256 signature over the raw
# request body, so server/webhooks.py is the entire authentication boundary
# here and it fails closed when no secret is configured.

@app.post("/webhooks/github")
async def github_webhook(request: Request) -> dict:
    """Ingest one verified GitHub delivery.

    Read the RAW body and verify BEFORE parsing: parse-then-verify would hand
    the JSON parser unsigned attacker input, which is the whole attack.

    An unregistered repo is accepted and dropped rather than 404'd. Two
    reasons: GitHub retries anything that is not 2xx, so a 404 would earn an
    endless redelivery loop for a repo nobody asked us to watch; and a
    distinguishable response would turn this endpoint into a registration
    oracle, letting anyone holding the secret enumerate which repos a team has
    connected.
    """
    raw = await request.body()
    if not verify_signature(
        settings.github_webhook_secret,
        raw,
        request.headers.get("X-Hub-Signature-256"),
    ):
        # No detail in the body: a stranger learns nothing from a refusal.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad signature")

    try:
        body = json.loads(raw)
    except ValueError:
        # Signed but unparseable. Not worth a retry, so answer 2xx and drop.
        logger.warning("github webhook: signed body was not JSON")
        return {"status": "ignored", "reason": "unparseable"}

    event = request.headers.get("X-GitHub-Event", "")
    installation_id = (body.get("installation") or {}).get("id")

    # An uninstall is the one delivery that must be acted on even though it
    # names no repository. Without it the rows survive, every later sync fails
    # on a revoked credential and burns its three retries, and the team's UI
    # goes on claiming a repository is connected that nothing can read.
    if event == "installation" and body.get("action") in ("deleted", "suspend"):
        if installation_id:
            await run_in_threadpool(forget_installation, int(installation_id))
        return {"status": "disconnected"}

    full_name = (body.get("repository") or {}).get("full_name")
    if not full_name:
        return {"status": "ignored", "reason": "no repository"}

    # BY INSTALLATION FIRST. Two teams may legitimately connect the same public
    # repository, and the name lookup returns whichever row came back first —
    # which would route one team's activity into another team's wiki. The
    # installation id is unique across all teams, so it cannot be ambiguous.
    team_id = None
    if installation_id:
        team_id = await run_in_threadpool(
            resolve_team_for_installation, int(installation_id)
        )
    if team_id is None:
        team_id = await run_in_threadpool(resolve_team_for_repo, full_name)
    if team_id is None:
        return {"status": "ignored"}

    # A push changed the code the agent reads, so refresh the checkout now
    # rather than waiting for the reconciler's window. Deduped by
    # enqueue_sync, so a burst of pushes queues one clone. Best-effort: the
    # activity ingest below is the delivery's actual job, and a sync that
    # cannot be queued is picked up by sweep_stale_checkouts anyway.
    if event == "push":
        try:
            await run_in_threadpool(enqueue_sync, team_id, full_name)
        except Exception:  # noqa: BLE001
            logger.exception("could not queue a sync for %s", full_name)

    job_id = await run_in_threadpool(
        enqueue_github_event,
        team_id,
        event,
        request.headers.get("X-GitHub-Delivery"),
        body,
    )
    return {"status": "queued", "job_id": job_id}


# ---------- documents ----------

@app.post("/documents/{document_id}/ingest")
def document_ingest(
    document_id: str,
    team_id: str,
    user_id: CurrentUserId,
    file: UploadFile = File(...),
) -> dict:
    """Queue an already-inserted `documents` row for parsing + compilation.

    Members cannot write `jobs` (that table is backend-only), so this endpoint
    is the bridge. The client uploads to Storage and inserts the row itself
    under RLS, then posts the same bytes here; v1 carries content inline in the
    job payload, matching handle_document_job. Fetching by storage_path instead
    is a later slice that removes the double upload.
    """
    require_membership(user_id, team_id)
    with user_session(user_id) as conn:
        row = conn.execute(
            "select kind from public.documents"
            " where id=%s and team_id=%s and deleted_at is null",
            (document_id, team_id),
        ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    kind = row[0]

    raw = file.file.read()
    if kind in _BINARY_KINDS:
        content = base64.b64encode(raw).decode("ascii")
    else:
        content = raw.decode("utf-8", errors="replace")

    try:
        job_id = enqueue_document(team_id, document_id, kind, content)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"job_id": job_id}
