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

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from agent.runtime import run_turn_sync
from pipeline.compiler import enqueue_document
from server.auth import CurrentUserId, require_membership
from shared.config import settings
from shared.consent import (
    ConsentError, approve_consent, edit_and_approve, reject_consent,
)
from shared.db import Role, team_session, user_session

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
    """Insert the reply as the AI itself — never attributed to the requester."""
    with team_session(Role.AGENT, team_id) as conn:
        row = conn.execute(
            "insert into public.messages (team_id, thread_type, thread_owner_id,"
            " sender_kind, body) values (%s,%s,%s,'ai',%s) returning id",
            (team_id, thread_type, owner_id, text),
        ).fetchone()
    return str(row[0])


@app.post("/agent/turn", response_model=TurnResponse)
def agent_turn(req: TurnRequest, user_id: CurrentUserId) -> TurnResponse:
    """Run one agent turn and persist both sides of it to `messages`.

    A direct reply to an explicit invocation is not consent-gated: the member
    asked, and the answer is visible to exactly the people who could already see
    the question. Actions the agent wants to take on top of that still go
    through propose_action -> the consent queue.
    """
    require_membership(user_id, req.team_id)
    user_message_id = _persist_user_message(
        user_id, req.team_id, req.thread_type, req.text
    )
    result = run_turn_sync(req.team_id, user_id, req.text)
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
    consent_id: str, req: TeamScoped, user_id: CurrentUserId
) -> dict:
    require_membership(user_id, req.team_id)
    return _consent_result(reject_consent(req.team_id, consent_id, user_id))


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
