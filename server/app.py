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
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from agent.runtime import run_turn_sync, stream_turn
from pipeline.compiler import enqueue_document
from pipeline.github import enqueue_github_event, resolve_team_for_repo
from server.auth import CurrentUserId, require_membership
from server.invites import invite_member
from server.webhooks import verify_signature
from shared.config import settings
from shared.consent import (
    ConsentError, approve_consent, edit_and_approve, reject_consent,
)
from shared.db import Role, team_session, user_session

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
def health() -> dict[str, str]:
    return {"status": "ok"}


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

    full_name = (body.get("repository") or {}).get("full_name")
    if not full_name:
        return {"status": "ignored", "reason": "no repository"}

    team_id = await run_in_threadpool(resolve_team_for_repo, full_name)
    if team_id is None:
        return {"status": "ignored"}

    job_id = await run_in_threadpool(
        enqueue_github_event,
        team_id,
        request.headers.get("X-GitHub-Event", ""),
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
