"""HTTP entrypoint for the Comrade agent.

Most product data is accessed directly from the browser under RLS. This
service holds server-only capabilities and credentials.

Identity always comes from the verified Supabase JWT (see server/auth.py); the
model never receives team_id / requester_id as tool arguments.
"""
import base64
import hashlib
import json
from pathlib import Path

import hmac

import psycopg
import logging
import uuid
import asyncio
import contextlib
import time

import websockets

import httpx

from fastapi import (
    Depends, FastAPI, File, Header, HTTPException, Request, UploadFile,
    WebSocket,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.background import BackgroundTask

from agent import processes
from agent.run_queue import cancel_run, enqueue_turn, get_run
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
from server import previews
from server.auth import CurrentUserId, require_membership
from server.invites import invite_member
from server.webhooks import verify_signature
from shared.config import settings
from shared.consent import (
    ConsentError, approve_consent, edit_and_approve, propose_action,
    reject_consent, revoke_permission_grant,
)
from pipeline import chat
from shared import observability
from shared.errors import safe_error
from shared.heartbeat import STALE_SECONDS as WORKER_STALE_SECONDS
from shared.heartbeat import live_workers
from shared.db import Role, connect, runtime_urls, team_session, user_session
from shared.agent_runs import get_thread_runs
from shared.usage import (
    BudgetExceeded, finalize_usage, record_reservation, release_turn, reserve_turn,
)

# At import rather than in a main(): the API is started by uvicorn, which
# never calls one. Without this the handler is uvicorn's own — no correlation
# fields and, more to the point, no redaction.
observability.setup("api")

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
    thread_id: uuid.UUID
    #: Identifies the ATTEMPT, not the text, and survives a retry.
    #: Without it an accepted POST whose connection then died looked exactly
    #: like one that never arrived, and the member sent their question twice.
    client_request_id: str | None = None


class TurnResponse(BaseModel):
    run_id: str
    status: str


class EditApproveRequest(TeamScoped):
    args: dict


class ApproveRequest(TeamScoped):
    grant_for_thread: bool = False


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
        with connect(Role.CONTROL) as conn:
            conn.execute("select 1")
    except Exception as exc:  # noqa: BLE001 - any failure to reach it counts
        logger.warning("health check could not reach the database: %s", exc)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "database": "unreachable"}
    return {"status": "ok", "database": "ok"}


@app.get("/ready")
def ready(response: Response) -> dict:
    """Can this deployment actually serve a turn, as opposed to answer a ping.

    /health says the process is alive and can reach Postgres. That is what an
    orchestrator should restart on. READINESS is a different question, and
    conflating them is how a deploy goes green while nothing works: the schema
    is a version behind, or no worker is draining the queue, so every turn a
    member sends is accepted and then sits there.

    Each check reports its own verdict. A single boolean would tell an operator
    that something is wrong and nothing about which thing.
    """
    checks: dict[str, str] = {}

    try:
        with connect(Role.CONTROL) as conn:
            conn.execute("select 1")
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 - any failure to reach it counts
        logger.warning("readiness: database unreachable: %s", safe_error(exc))
        checks["database"] = "unreachable"

    # 🔴 Only the CONTROL role was ever checked — the one the API answers with.
    # A turn needs `comrade_agent`, an approved action needs
    # `comrade_executor`, a document needs `comrade_pipeline` and a member read
    # needs `comrade_authenticator`, so a half-done credential rotation left
    # this green while every turn in the deployment failed.
    #
    # Direct connections rather than the pools: the question is whether the
    # CREDENTIAL works, and a pool borrow on a bad one waits out its whole
    # timeout before saying so — a readiness probe that hangs is a readiness
    # probe that gets killed.
    broken = []
    for name, url in runtime_urls().items():
        if not url:
            broken.append(f"{name}: not configured")
            continue
        try:
            with psycopg.connect(url, connect_timeout=READY_CONNECT_SECONDS) as conn:
                conn.execute("select 1")
        except Exception as exc:  # noqa: BLE001
            broken.append(f"{name}: {safe_error(exc)}")
    checks["roles"] = "ok" if not broken else "; ".join(broken)

    if checks["database"] == "ok":
        checks["migrations"] = _migration_check()
        checks["agent_queue"] = _queue_check(
            "select count(*) from public.agent_runs where status='queued'"
            " and created_at < now() - make_interval(mins => %s)",
            "run(s) queued over {mins}m — is the agent worker running?",
        )
        # 🔴 Not checked at all. A wedged pipeline worker means no document is
        # ever compiled and no memory ever written — silently, behind a green
        # deploy, because the agent queue it did check was moving fine.
        checks["pipeline_queue"] = _queue_check(
            "select count(*) from public.jobs where status='pending'"
            " and available_at < now() - make_interval(mins => %s)",
            "job(s) pending over {mins}m — is the pipeline worker running?",
        )
        # Work a dead worker is holding that nobody is doing. The recovery
        # sweeps are supposed to reclaim these; a pile of them means the sweep
        # itself is not running, which no queue-depth check would show.
        checks["expired_leases"] = _lease_check()
        # 🔴 Everything above answers "is work stuck". None of it answers "is
        # anybody here": on a quiet deployment both queues are empty, so a
        # stack with BOTH WORKERS STOPPED reported itself ready and the first
        # member to send a message found out (fix.md F33).
        checks["workers"] = _worker_check()
        checks["sandbox"] = _sandbox_check()

    ok = all(v == "ok" for v in checks.values())
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if ok else "not_ready", "checks": checks}


@app.get("/metrics")
def metrics(authorization: str = Header(default="")) -> dict:
    """Counts and ages, for deciding whether a limit is set right.

    🔴 Nothing counted anything. `/ready` answers "is it broken", which is the
    wrong question for every decision an operator actually makes: whether the
    hourly token cap is too tight, whether the compiler is falling behind, how
    many turns ended badly and in which way. Each of those was a SQL query
    somebody had to write from memory against a schema they did not have in
    front of them, at the moment they could least afford it.

    AGGREGATES ONLY, and no strings from any row. An operations endpoint that
    lists which team is spending what is a cross-team disclosure wearing a
    monitoring hat, and one that reports `last_error` undoes the redaction in
    shared/errors.py by another route.
    """
    expected = settings.comrade_metrics_token
    if not expected:
        # 404, not 401: an endpoint that is not turned on should not advertise
        # that it exists.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    supplied = authorization.removeprefix("Bearer ").strip()
    # Constant time. A metrics token is guessable by timing exactly like any
    # other secret, and `==` on strings returns early on the first differing
    # byte.
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authorised")

    with connect(Role.CONTROL) as conn:
        pipeline = conn.execute(
            "select"
            "  count(*) filter (where status='pending'),"
            "  count(*) filter (where status='processing'),"
            "  count(*) filter (where status='failed'),"
            # The number `/ready`'s threshold is compared against. An operator
            # choosing that threshold needs to see it.
            "  coalesce(extract(epoch from now() -"
            "    min(created_at) filter (where status='pending')), 0),"
            # Work reclaimed from a worker that stopped answering. A rising
            # count is how an OOM-killed worker is found when nothing else says
            # so.
            "  count(*) filter (where status='processing'"
            "                     and lease_expires_at < now()),"
            "  coalesce(max(attempts), 0)"
            " from public.jobs"
        ).fetchone()
        runs = conn.execute(
            "select status, count(*) from public.agent_runs"
            " where created_at > now() - interval '24 hours' group by status"
        ).fetchall()
        # How far behind the wiki is from the conversation. Nothing FAILS when
        # this grows; the product just quietly stops knowing things.
        #
        # 🔴 Two defects here. It compared against `max(chat_through)` across
        # ALL teams, so one team compiling five minutes ago made every older
        # message everywhere look already-compiled — the team whose pipeline
        # had been broken for a week was the one it could not see. And it
        # counted every message, while capture only ever looks at undeleted
        # USER messages in TEAM-VISIBLE threads, so a team whose conversation
        # is entirely private reported a backlog that no working pipeline
        # could ever clear.
        #
        # Per team, over what capture would actually take, using capture's own
        # definition (pipeline/chat.py) so the two cannot drift.
        lag = conn.execute(
            "select coalesce(max(extract(epoch from now() - m.created_at)), 0)"
            "  from public.messages m"
            "  join public.threads th"
            "    on th.id = m.thread_id and th.team_id = m.team_id"
            " where " + chat.CAPTURABLE_SQL +
            "   and m.created_at >" + chat.CAPTURED_THROUGH_SQL.format(
                team="m.team_id")
        ).fetchone()[0]

        # Summed here rather than listed per team: an operations endpoint
        # that says which team is spending what is a cross-team disclosure
        # wearing a monitoring hat.
        usage = conn.execute(
            "select coalesce(sum(turns),0), coalesce(sum(tokens),0),"
            "       count(*) from public.usage_buckets"
            " where bucket = date_trunc('hour', now())"
        ).fetchone()

    return {
        "pipeline": {
            "pending": pipeline[0],
            "processing": pipeline[1],
            "failed": pipeline[2],
            "oldest_pending_seconds": int(pipeline[3]),
            "leases_expired": pipeline[4],
            "max_attempts_seen": pipeline[5],
            "compiler_lag_seconds": int(lag),
        },
        "agent": {
            "runs_by_status": {status: count for status, count in runs},
        },
        "usage": {
            "turns_this_hour": int(usage[0]),
            "tokens_this_hour": int(usage[1]),
            "teams_active_this_hour": usage[2],
            # A number with nothing to compare it to is not a metric.
            "turns_cap": settings.agent_turns_per_hour,
            "tokens_cap": settings.agent_tokens_per_hour,
            "turn_estimate": settings.agent_tokens_estimate,
        },
    }


def _migration_check() -> str:
    """Every migration the code carries, against every one the database has.

    🔴 This compared MAXIMUMS — `applied >= newest_on_disk` — so a release that
    skipped one in the middle passed on the strength of the newest one being
    there. A migration that failed and was retried, a rebase that reordered two
    files, a partially restored backup: each leaves a gap below the top, and
    the column nobody created is then a 500 on whichever request first touches
    it, which reads as a code bug rather than a half-finished release.
    """
    try:
        on_disk = {
            path.name.split("_", 1)[0]
            for path in (Path(__file__).resolve().parent.parent
                         / "supabase" / "migrations").glob("*.sql")
        }
        with connect(Role.CONTROL) as conn:
            applied = {
                row[0] for row in conn.execute(
                    "select version from supabase_migrations.schema_migrations"
                ).fetchall()
            }
    except Exception as exc:  # noqa: BLE001
        return f"unknown: {safe_error(exc)}"
    missing = sorted(on_disk - applied)
    if not missing:
        return "ok"
    # Named, not counted: "3 missing" sends an operator to diff two lists by
    # hand at the moment they can least afford it.
    shown = ", ".join(missing[:READY_MAX_LISTED])
    more = f" (+{len(missing) - READY_MAX_LISTED} more)"         if len(missing) > READY_MAX_LISTED else ""
    return f"not applied: {shown}{more}"


def _queue_check(sql: str, complaint: str) -> str:
    """Is work STUCK — not "is work happening". An empty queue is the normal
    state of a healthy deployment and must never read as a stall."""
    try:
        with connect(Role.CONTROL) as conn:
            stalled = conn.execute(sql, (READY_STALL_MINUTES,)).fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        return f"unknown: {safe_error(exc)}"
    if not stalled:
        return "ok"
    return f"{stalled} " + complaint.format(mins=READY_STALL_MINUTES)


def _worker_check() -> str:
    """Both workers, present and recent, whether or not there is work."""
    try:
        live = live_workers()
    except Exception as exc:  # noqa: BLE001
        return f"unknown: {safe_error(exc)}"
    missing = [kind for kind in ("agent", "pipeline") if not live.get(kind)]
    if missing:
        return (
            f"no {' or '.join(missing)} worker has reported in the last"
            f" {WORKER_STALE_SECONDS}s"
        )
    return "ok"


def _sandbox_check() -> str:
    """What the execution worker says it can run code with.

    Reported BY the worker rather than probed here: the API holds no Docker
    socket on purpose, so it cannot find this out for itself, and giving it one
    to answer a health check would turn a request-handling bug into a host
    compromise.
    """
    try:
        agents = live_workers().get("agent") or []
    except Exception as exc:  # noqa: BLE001
        return f"unknown: {safe_error(exc)}"
    if not agents:
        return "unknown: no agent worker is reporting"
    states = {
        (worker["capabilities"] or {}).get("sandbox", "unreported")
        for worker in agents
    }
    if states == {"ok"}:
        return "ok"
    # Named honestly, including "unsupported" for a backend nothing here has
    # been proven against.
    return "; ".join(sorted(states))


def _lease_check() -> str:
    """Work a dead worker is holding that nobody is doing.

    🔴 This counted JOBS only. An agent run holds a lease the same way and is
    recovered by the same kind of sweep, and a member watching a turn that
    stopped mid-flight is a louder failure than a document that has not been
    parsed — yet it was the half that went unchecked.
    """
    try:
        with connect(Role.CONTROL) as conn:
            jobs = conn.execute(
                "select count(*) from public.jobs where status='processing'"
                " and lease_expires_at < now() - make_interval(mins => %s)",
                (READY_STALL_MINUTES,),
            ).fetchone()[0]
            runs = conn.execute(
                "select count(*) from public.agent_runs where status='running'"
                " and lease_expires_at < now() - make_interval(mins => %s)",
                (READY_STALL_MINUTES,),
            ).fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        return f"unknown: {safe_error(exc)}"
    stuck = []
    if jobs:
        stuck.append(f"{jobs} job(s)")
    if runs:
        stuck.append(f"{runs} agent run(s)")
    if not stuck:
        return "ok"
    return (" and ".join(stuck) + " held past an expired lease for over"
            f" {READY_STALL_MINUTES}m — is the recovery sweep running?")


#: How long a run may sit queued before readiness calls the queue stalled.
#: Longer than the slowest legitimate turn, so a busy worker is never reported
#: as a missing one.
READY_STALL_MINUTES = 10
#: A readiness probe that hangs is a readiness probe that gets killed, and an
#: unreachable role is exactly the case where a connect can hang.
READY_CONNECT_SECONDS = 3
#: Missing migrations are NAMED rather than counted — "3 missing" sends an
#: operator to diff two lists by hand at the moment they can least afford it —
#: but a fresh database is missing all of them, and that is not a report.
READY_MAX_LISTED = 5


# ---------- agent ----------

def _resolve_thread(
    user_id: str, team_id: str, thread_id: str | uuid.UUID
) -> str:
    """Resolve one canonical, requester-visible thread."""
    with user_session(user_id) as conn:
        row = conn.execute(
            "select id from public.threads where id=%s and team_id=%s",
            (thread_id, team_id),
        ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "thread not found")
    return str(row[0])

def _persist_user_message(
    user_id: str, team_id: str, thread_id: str, text: str
) -> str:
    """Insert the member's message as themselves, so RLS authorises the write."""
    with user_session(user_id) as conn:
        row = conn.execute(
            "insert into public.messages (team_id, thread_id, sender_kind, sender_id, body)"
            " values (%s,%s,'user',%s,%s)"
            " returning id",
            (team_id, thread_id, user_id, text),
        ).fetchone()
    return str(row[0])


def _persist_ai_reply(
    team_id: str, thread_id: str, text: str
) -> str:
    """Insert the reply as the AI itself — never attributed to the requester.

    The id is generated here rather than with RETURNING: RETURNING needs SELECT
    on `messages`, and findings §4.1 revoked the agent's read of that table.
    """
    message_id = str(uuid.uuid4())
    with team_session(Role.AGENT, team_id) as conn:
        conn.execute(
            "insert into public.messages (id, team_id, thread_id, sender_kind, body)"
            " values (%s,%s,%s,'ai',%s)",
            (message_id, team_id, thread_id, text),
        )
    return message_id


def _admit_turn(req, user_id: str):
    """Reserve budget, authorise the thread, and persist the turn — in that
    order, releasing what was reserved if any later step refuses.

    🔴 THE ORDER IS THE FIX. This was `_check_turn_budget` — a count and a sum
    over agent_runs compared to the cap — and the comparison happened in a
    different transaction from the spend. Two simultaneous turns both read a
    snapshot under the cap and both proceeded; nothing downstream stopped the
    second. The cap was advice, and on a deployment where the model key is the
    operator's, that is somebody else's bill.

    The reservation is now a single conditional UPDATE on one row
    (shared/usage.py), so concurrent turns serialise on that row's lock.
    Reserved BEFORE the message is persisted, because a turn admitted is a
    turn that will cost money whether or not the rest of this function works.
    """
    try:
        reservation = reserve_turn(req.team_id)
    except BudgetExceeded as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, exc.detail
        ) from exc
    try:
        thread_id = _resolve_thread(user_id, req.team_id, req.thread_id)
        turn = enqueue_turn(
            req.team_id, user_id, thread_id, req.text,
            client_request_id=req.client_request_id,
        )
    except BaseException:
        # An inaccessible thread, a database error, a disconnect. None of them
        # spent anything, and leaving the estimate on the bucket would refuse
        # a team work it never did.
        release_turn(req.team_id, reservation)
        raise
    if getattr(turn, "status", "") == "duplicate":
        # A retry of a turn already admitted. It was charged the first time;
        # charging it again lets a flaky connection eat a team's hourly budget
        # without asking the model one extra question.
        release_turn(req.team_id, reservation)
        return turn
    record_reservation(req.team_id, str(turn), reservation)
    return turn


@app.post("/agent/turn", response_model=TurnResponse)
def agent_turn(req: TurnRequest, user_id: CurrentUserId) -> TurnResponse:
    """Persist a turn and return immediately; agent.worker executes it."""
    require_membership(user_id, req.team_id)
    turn = _admit_turn(req, user_id)
    return TurnResponse(run_id=str(turn), status=getattr(turn, "status", "queued"))


#: The run is over and will produce nothing more.
TERMINAL_STATUSES = {"done", "failed", "cancelled"}
#: The run is parked on a human decision. Emphatically NOT finished — these
#: arrived as `done` too, so a run waiting on a consent card stopped the typing
#: indicator and read to the member as a turn that silently failed, while the
#: card asking for the very permission that would continue it sat on screen.
WAITING_STATUSES = {"waiting_for_permission", "waiting_for_user"}

#: 🔴 The poll was a flat 200ms. A run waiting on a slow model call cost the
#: database exactly as much as one producing a step every tick, and every
#: viewer paid it independently. It now backs off while nothing happens and
#: snaps back the moment something does.
POLL_START_SECONDS = 0.2
POLL_MAX_SECONDS = 2.0
POLL_GROWTH = 1.5
#: Backing off must not make a dead connection look like a quiet one. After
#: this much silence something goes down the wire so both ends can tell.
#: Measured in the wait the loop ASKED for rather than wall clock, so the
#: heartbeat keeps its meaning when the loop is driven by a test.
HEARTBEAT_SECONDS = 15.0


async def _run_frames(team_id: str, run_id: str, *, viewer_id: str,
                     after_seq: int = -1):
    """Replay durable steps from a cursor until the run stops producing them.

    `after_seq` is the highest sequence the browser has already rendered.
    Replaying from zero is right after a refresh — nothing is on screen — and
    wrong after a dropped connection, where it duplicates every tool card the
    member is already looking at. It is also what is asked of the DATABASE:
    the read starts after the cursor rather than fetching the whole run and
    filtering afterwards.

    🔴 `viewer_id` was not here at all. Membership and thread visibility were
    checked once, when the connection opened, and every read after that was
    privileged and unattributed — the generator asked the database for the run
    by id and streamed whatever came back. A member removed from the team, or
    removed from a restricted thread's participants, kept receiving new private
    steps for as long as they left the tab open, which on a long turn is the
    whole turn. Revocation has to reach a stream that is already running.
    """
    seen = after_seq + 1
    status: str | None = None
    wait = POLL_START_SECONDS
    idle = 0.0
    while True:
        run = await run_in_threadpool(get_run, team_id, run_id, seen - 1)
        if run is None:
            yield json.dumps({"type": "error", "detail": "run not found"}) + "\n"
            return
        # Guarding DISCLOSURE rather than every poll. Fetching is a privileged
        # read the viewer never sees; what needs authorizing is the yield. Most
        # polls of a live run produce nothing — that is what the backoff is for
        # — so checking only when there is something to send costs nothing on
        # the idle path and still leaves no window: no batch is emitted after
        # access ends, not even the one already in hand.
        has_news = (
            status is None
            or run["status"] != status
            or any(step["seq"] >= seen for step in run["steps"])
        )
        if has_news and not await run_in_threadpool(
            _may_watch, team_id, viewer_id, run_id
        ):
            yield json.dumps({
                "type": "error", "detail": "access to this run has ended",
            }) + "\n"
            return
        sent = False
        if status is None:
            # Opened with the REAL status, not an assumed "queued": a reattach
            # needs to know whether it is resuming live work or reading history.
            status = run["status"]
            sent = True
            yield json.dumps(
                {"type": "run", "run_id": run_id, "status": status}
            ) + "\n"
        elif run["status"] != status:
            # Lifecycle, on its own frame type. Queued and running are
            # different things to wait through, and the room can now say which.
            status = run["status"]
            sent = True
            yield json.dumps(
                {"type": "status", "run_id": run_id, "status": status}
            ) + "\n"
        for step in run["steps"]:
            if step["seq"] >= seen:
                seen = step["seq"] + 1
                sent = True
                yield json.dumps(step) + "\n"
        if status in WAITING_STATUSES or status in TERMINAL_STATUSES:
            yield json.dumps({
                "type": "done" if status in TERMINAL_STATUSES else "status",
                "run_id": run_id, "status": status, "detail": run["last_error"],
            }) + "\n"
            return
        if sent:
            # Something happened, so the next thing probably will too.
            wait = POLL_START_SECONDS
            idle = 0.0
        else:
            wait = min(wait * POLL_GROWTH, POLL_MAX_SECONDS)
            if idle >= HEARTBEAT_SECONDS:
                idle = 0.0
                yield json.dumps({"type": "heartbeat", "run_id": run_id}) + "\n"
        idle += wait
        await asyncio.sleep(wait)


def _may_watch(team_id: str, user_id: str, run_id: str) -> bool:
    """Whether this viewer may still see this run, right now.

    The boolean form of `_visible_run`, for the streaming path: a generator
    mid-flight wants to close cleanly rather than raise an HTTPException into
    a response whose headers were sent long ago.
    """
    try:
        _visible_run(team_id, user_id, run_id)
    except HTTPException:
        return False
    try:
        require_membership(user_id, team_id)
    except HTTPException:
        return False
    return True


def _visible_run(team_id: str, user_id: str, run_id: str) -> None:
    run = get_run(team_id, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    _resolve_thread(user_id, team_id, run["thread_id"])


@app.post("/agent/turn/stream")
async def agent_turn_stream(req: TurnRequest, user_id: CurrentUserId):
    """Compatibility shortcut: enqueue, then follow the durable event log."""
    # 🔴 Called directly on the event loop. It is a DATABASE round trip, and
    # every other request this worker was serving waited behind it — on the
    # streaming endpoints, which are the ones a room holds open.
    await run_in_threadpool(require_membership, user_id, req.team_id)
    turn = await run_in_threadpool(_admit_turn, req, user_id)
    return StreamingResponse(
        _run_frames(req.team_id, str(turn), viewer_id=user_id),
        media_type="application/x-ndjson",
    )


@app.get("/agent/runs/{run_id}/stream")
async def agent_run_stream(
    run_id: str, team_id: str, user_id: CurrentUserId, after_seq: int = -1,
):
    """Follow a durable run. This is how the browser watches one.

    Reattaching is the same call as attaching, so a refresh, a reconnect and a
    fresh send take one path instead of the POST-only path that lost the run
    the moment its response ended.
    """
    await run_in_threadpool(require_membership, user_id, team_id)
    await run_in_threadpool(_visible_run, team_id, user_id, run_id)
    return StreamingResponse(
        _run_frames(team_id, run_id, viewer_id=user_id, after_seq=after_seq),
        media_type="application/x-ndjson",
    )


@app.post("/agent/runs/{run_id}/cancel")
def agent_run_cancel(run_id: str, req: TeamScoped, user_id: CurrentUserId) -> dict:
    """Stop a turn.

    🔴 There was no way to. `cancel_run` sat in the queue module with nothing
    reaching it, so a member who asked the wrong question, or watched a turn
    head somewhere expensive, could only wait it out.

    Two checks, and they are different questions. Thread access says the run is
    yours to SEE — RLS-equivalent, the same check every other run read makes.
    Requester ownership says it is yours to STOP: a teammate watching your turn
    must not be able to speak for you and end it. The ownership half is
    enforced inside the UPDATE rather than read first and written after,
    because a check in a different transaction from the write can be raced.
    """
    require_membership(user_id, req.team_id)
    run = get_run(req.team_id, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    _resolve_thread(user_id, req.team_id, run["thread_id"])
    if run["requester_id"] != user_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "only the member who asked for this turn can stop it",
        )
    stopped = cancel_run(req.team_id, run_id, requester_id=user_id)
    if stopped and run["status"] == "queued":
        # It never reached the model, so it must not count against the hour.
        # A running turn settles on its own way out, with the real number.
        finalize_usage(req.team_id, run_id, 0)
    return {"status": "cancelled", "already_finished": not stopped}


@app.get("/threads/{thread_id}/agent-runs")
def thread_agent_runs(thread_id: str, team_id: str, user_id: CurrentUserId) -> list[dict]:
    """Return activity only after RLS-equivalent thread access is checked."""
    require_membership(user_id, team_id)
    resolved = _resolve_thread(user_id, team_id, thread_id)
    return get_thread_runs(team_id, resolved)


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
    consent_id: str, req: ApproveRequest, user_id: CurrentUserId
) -> dict:
    require_membership(user_id, req.team_id)
    try:
        outcome = (
            approve_consent(req.team_id, consent_id, user_id, grant_for_thread=True)
            if req.grant_for_thread else approve_consent(req.team_id, consent_id, user_id)
        )
        return _consent_result(outcome)
    except ConsentError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@app.post("/permission-grants/{grant_id}/revoke")
def permission_grant_revoke(
    grant_id: str, req: TeamScoped, user_id: CurrentUserId
) -> dict:
    require_membership(user_id, req.team_id)
    if not revoke_permission_grant(req.team_id, grant_id, user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "permission grant not found")
    return {"status": "revoked"}


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
    ("threads", "select * from public.threads where team_id = %(team)s order by created_at"),
    ("thread_participants", "select * from public.thread_participants where team_id = %(team)s order by joined_at"),
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

    # The target alone can read and resolve this card. Reuse the existing
    # private-thread helper, which creates their canonical restricted thread
    # only when an older account does not already have one.
    with team_session(Role.AGENT, team_id) as conn:
        thread_id = conn.execute(
            "select public.ensure_private_thread(%s,%s)", (team_id, member_id),
        ).fetchone()[0]

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
        thread_id=str(thread_id),
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
            "select 1 from public.messages m join public.threads t"
            " on t.id=m.thread_id and t.team_id=m.team_id"
            " where m.id=%s and m.team_id=%s and t.visibility='team'"
            " and m.deleted_scope is null",
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
            " join public.threads t on t.id=m.thread_id and t.team_id=m.team_id"
            " where m.id=%s and m.team_id=%s and m.sender_kind='ai'"
            " and t.visibility='team'"
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

    # BEFORE acknowledging. A check result that is only queued is a check
    # result that a worker crash loses, and the thread whose work failed never
    # hears about it — which is the whole defect this closes.
    # 🔴 TWO defects lived here. The delivery id was passed as a fifth
    # POSITIONAL argument to a keyword-only parameter, so every check event
    # raised TypeError, was swallowed by a broad `except`, and the delivery was
    # acknowledged with no result recorded — the feature T25 built never ran
    # once, and its tests called the helper directly so they stayed green over
    # it. And the reasoning for doing it inline was wrong: "a result that is
    # only queued is one a worker crash loses" is not true of a QUEUED JOB,
    # which is a durable row with retries and backoff. Logging and continuing
    # is what actually lost results.
    #
    # The job below IS the durable record. Correlation happens in its handler,
    # where a failure is retried instead of written to a log nobody reads.

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
    file: UploadFile | None = File(None),
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
            "select kind, storage_path from public.documents"
            " where id=%s and team_id=%s and deleted_at is null",
            (document_id, team_id),
        ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    kind, storage_path = row

    # 🔴 The bytes used to go into the job payload — base64 for pdf and docx —
    # and `comrade_control` can read `jobs.payload`. The cross-team maintenance
    # role could read every document every team had uploaded, because the queue
    # was carrying them past it. The file is already in private Storage; the
    # job says WHICH file and the worker fetches it under its own permission.
    if storage_path:
        digest = None
        if file is not None:
            # Identity only. The bytes are dropped here; what survives is a
            # hash the worker can use to notice the file changed underneath it.
            digest = hashlib.sha256(file.file.read()).hexdigest()
        try:
            job_id = enqueue_document(
                team_id, document_id, kind,
                storage_path=storage_path, content_sha256=digest,
            )
        except LookupError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        return {"job_id": job_id}

    # No stored file: older rows, and anything uploaded straight to the API.
    # Still inline, still the old shape, and the only path that keeps a
    # document in the queue.
    if file is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this document has no stored file — upload it again",
        )
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


@app.post("/documents/{document_id}/reingest")
def document_reingest(
    document_id: str, req: TeamScoped, user_id: CurrentUserId,
) -> dict:
    """Parse a document again from what is already stored.

    🔴 The only way to retry was `/ingest`, which takes the bytes as multipart
    — so a member whose ingestion failed had to find the file and upload it a
    second time, and after a reload the browser no longer had it at all. They
    were told what went wrong and offered nothing to do about it, for a file
    the system was already holding.
    """
    require_membership(user_id, req.team_id)
    with user_session(user_id) as conn:
        row = conn.execute(
            "select kind, storage_path from public.documents"
            " where id=%s and team_id=%s and deleted_at is null",
            (document_id, req.team_id),
        ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    kind, storage_path = row
    if not storage_path:
        # Older rows exist with no stored file. "Retry" cannot mean anything
        # for them, and saying so is better than a confusing failure later.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this document has no stored file to read — upload it again",
        )

    # By REFERENCE, like the first parse. The endpoint no longer reads the
    # file at all: the worker fetches it when it is ready, under its own
    # permission, and the queue never carries a team's document.
    try:
        job_id = enqueue_document(
            req.team_id, document_id, kind, storage_path=storage_path,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"job_id": job_id}


# ---------- previews ----------
# The one route that forwards a request into a team's own unreviewed code.
# server/previews.py is the boundary; this is the plumbing around it.

#: A development server can serve a large bundle, but nothing here should be
#: streaming a video. Bounded because the body is buffered, and an unbounded
#: buffer is a memory exhaustion vector any repository could trigger.
PREVIEW_MAX_BYTES = 25 * 1024 * 1024
PREVIEW_TIMEOUT = 30.0


class PreviewGrant(BaseModel):
    url: str
    expires_in: int


@app.post("/previews/{process_id}", response_model=PreviewGrant)
def preview_grant(process_id: str, team_id: str, user_id: CurrentUserId) -> PreviewGrant:
    """Mint a short-lived preview link for a process this member can see."""
    require_membership(user_id, team_id)
    try:
        launch = previews.launch(user_id, team_id, process_id)
    except previews.PreviewUnconfigured as exc:
        # 503 and the reason: an operator sees "previews are not configured"
        # rather than a member seeing a broken button with no explanation.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)
        ) from exc
    except previews.PreviewDenied as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    # An ABSOLUTE url on the preview origin. The browser leaves this origin
    # entirely, which is the point.
    return PreviewGrant(
        url=launch["url"],
        expires_in=settings.comrade_preview_grant_seconds,
    )




# ---------- preview origins ----------
#
# Requests arriving on a preview hostname are handled HERE, before any
# application route sees them. A preview host is a different site from the app:
# it carries no Supabase token and no Comrade cookie, only the host-scoped
# preview cookie set by the launch exchange below.
#
# 🔴 The same-origin `/previews/<id>/<path>` proxy that used to live on the app
# host is GONE. It served a team's unreviewed development server from Comrade's
# own origin, where its JavaScript could read the member's session out of
# localStorage. It is not deprecated or disabled behind a flag — it is removed,
# because a route that can be re-enabled is a route that will be.


async def _serve_preview(request: Request, host: str, process_id: str) -> Response:
    """Everything on a preview hostname."""
    if request.url.path == "/__comrade/launch":
        try:
            redeemed = await run_in_threadpool(
                previews.redeem, request.query_params.get("grant", ""), host=host,
            )
        except previews.PreviewDenied as exc:
            return Response(str(exc), status_code=status.HTTP_403_FORBIDDEN)
        cookie = previews.session_cookie(redeemed)
        # 303 to the root: the grant is spent, and leaving it in the address bar
        # would put a consumed credential in the member's history.
        response = Response(status_code=status.HTTP_303_SEE_OTHER,
                            headers={"location": "/"})
        response.set_cookie(
            cookie["key"], cookie["value"], max_age=cookie["max_age"],
            httponly=cookie["httponly"], secure=cookie["secure"],
            samesite=cookie["samesite"], path=cookie["path"],
        )
        return response

    try:
        grant = await run_in_threadpool(
            previews.authorize_session,
            request.cookies.get("comrade_preview", ""), host=host,
        )
    except previews.PreviewDenied as exc:
        return Response(str(exc), status_code=status.HTTP_403_FORBIDDEN)
    if grant["process_id"] != process_id:
        return Response("that preview is not available.",
                        status_code=status.HTTP_403_FORBIDDEN)

    # Record the use. Idle expiry means idle, and without this a preview
    # somebody is actively looking at dies mid-session because nothing said so.
    await run_in_threadpool(processes.touch, grant["team_id"], process_id)

    body = await request.body()
    if len(body) > PREVIEW_MAX_BYTES:
        return Response("request too large",
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

    # The RAW query string, forwarded byte for byte. Re-encoding it from parsed
    # pairs is what loses a literal `+`, a repeated key, or a percent-encoded
    # `&` inside a value — and a development server that reads its own query
    # differently from the app is a bug nobody can reproduce.
    url = previews.upstream_url(grant, path=request.url.path, host=host)
    if request.url.query:
        url = f"{url}?{request.url.query}"

    client = httpx.AsyncClient(timeout=PREVIEW_TIMEOUT, follow_redirects=False)
    try:
        upstream_request = client.build_request(
            request.method, url,
            headers=previews.forwardable_headers(dict(request.headers)),
            content=body,
        )
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.info("preview upstream failed for %s: %s", process_id, exc)
        return Response(
            "the development server did not respond. It may still be starting,"
            " or it may have crashed — check the process logs in the thread.",
            status_code=status.HTTP_502_BAD_GATEWAY,
        )

    try:
        previews.enforce_response_limit(dict(upstream.headers), PREVIEW_MAX_BYTES)
    except previews.PreviewTooLarge as exc:
        await upstream.aclose()
        await client.aclose()
        return Response(str(exc), status_code=status.HTTP_502_BAD_GATEWAY)

    async def _body():
        """Stream RAW bytes, counting as we go.

        aiter_raw, not aiter_bytes: the body is forwarded undecoded and its
        content-encoding header travels with it, which is the only arrangement
        that cannot contradict itself. A chunked response with no declared
        length is cut off at the cap rather than buffered — the connection ends
        without a clean close, which a browser reports as a failed load instead
        of rendering half a file as though it were whole.
        """
        total = 0
        try:
            async for chunk in upstream.aiter_raw():
                total += len(chunk)
                if total > PREVIEW_MAX_BYTES:
                    logger.info(
                        "preview response for %s passed %d bytes; cutting off",
                        process_id, PREVIEW_MAX_BYTES,
                    )
                    return
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        _body(),
        status_code=upstream.status_code,
        headers=previews.response_headers(dict(upstream.headers)),
    )


@app.middleware("http")
async def preview_origin(request: Request, call_next):
    """Route by HOSTNAME before any application route matches.

    Caddy passes the original host in X-Comrade-Preview-Host; falling back to
    Host covers a direct connection. Either way the hostname only ever selects
    WHICH process — it never authorizes, which is done against the database on
    the next line down.
    """
    host = (request.headers.get("x-comrade-preview-host")
            or request.headers.get("host", ""))
    process_id = previews.process_for_host(host)
    if process_id is None:
        return await call_next(request)
    return await _serve_preview(request, host, process_id)


#: How long one proxied WebSocket may stay open. A development server's
#: hot-reload socket is meant to live as long as the tab, but an unbounded
#: connection is an unbounded worker task — and a preview whose participant was
#: removed must not keep streaming through a socket opened before they were.
#: The reauthorization below is what actually ends it; this is the ceiling.
PREVIEW_WS_SECONDS = 60 * 30

#: How often an open socket re-asks whether its holder still may be here.
#: The most memory ONE upstream message may make the API allocate.
#:
#: 🔴 This connection was opened with `max_size=None`, which turns the limit
#: off entirely. The far side of a preview socket is a development server
#: written by a model and running a team's own unreviewed code, and the reader
#: assembles a complete message before handing it over — so one frame declared
#: large enough exhausts the internet-facing process, whatever the HTTP body
#: limits elsewhere say.
#:
#: 4 MB: a Vite hot-reload update for a large module graph is tens of
#: kilobytes, so this is generous for the traffic it carries and small enough
#: that several concurrent previews cannot take the process down. The reverse
#: direction is bounded by uvicorn's own `--ws-max-size` (16 MB by default),
#: and that side is the member's browser rather than the team's code.
PREVIEW_WS_MAX_BYTES = 4 * 1024 * 1024
PREVIEW_WS_RECHECK_SECONDS = 60


@app.websocket("/{path:path}")
async def preview_websocket(websocket: WebSocket, path: str) -> None:
    """Proxy a WebSocket into a thread's development server.

    This is what makes hot reload work inside a preview. It used to be a 501,
    which loaded the page and then quietly never updated it.

    The socket is authorized the same way every HTTP request is — the
    host-scoped cookie, rechecked against the database — and then RE-checked
    periodically while it is open, because a connection established an hour ago
    says nothing about whether its holder is still in the thread.
    """
    host = (websocket.headers.get("x-comrade-preview-host")
            or websocket.headers.get("host", ""))
    process_id = previews.process_for_host(host)
    if process_id is None:
        await websocket.close(code=1008)
        return
    try:
        grant = await run_in_threadpool(
            previews.authorize_session,
            websocket.cookies.get("comrade_preview", ""), host=host,
        )
    except previews.PreviewDenied:
        await websocket.close(code=1008)
        return

    query = websocket.url.query
    target = (f"ws://{grant['container_name']}:{grant['port']}/"
              f"{path.lstrip('/')}" + (f"?{query}" if query else ""))
    await websocket.accept()

    try:
        async with websockets.connect(
            target, open_timeout=10, close_timeout=5,
            max_size=PREVIEW_WS_MAX_BYTES,
        ) as upstream:
            await _pump_websocket(websocket, upstream, host, grant)
    except (OSError, websockets.exceptions.WebSocketException) as exc:
        logger.info("preview websocket to %s failed: %s", process_id, exc)
        with contextlib.suppress(RuntimeError):
            await websocket.close(code=1011)


async def _pump_websocket(client_ws, upstream, host: str, grant: dict) -> None:
    """Copy frames both ways until one side stops, the lifetime ends, or the
    holder loses access."""
    deadline = time.monotonic() + PREVIEW_WS_SECONDS

    async def _client_to_upstream() -> None:
        while True:
            message = await client_ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            if (data := message.get("bytes")) is not None:
                await upstream.send(data)
            elif (text := message.get("text")) is not None:
                await upstream.send(text)

    async def _upstream_to_client() -> None:
        async for frame in upstream:
            if isinstance(frame, bytes):
                await client_ws.send_bytes(frame)
            else:
                await client_ws.send_text(frame)

    async def _keep_authorized() -> None:
        """Re-ask the database while the socket is open. A participant removed
        from the thread must lose a live connection, not merely be unable to
        open the next one."""
        while time.monotonic() < deadline:
            await asyncio.sleep(PREVIEW_WS_RECHECK_SECONDS)
            try:
                await run_in_threadpool(
                    previews.authorize_session,
                    client_ws.cookies.get("comrade_preview", ""), host=host,
                )
            except previews.PreviewDenied:
                return
        return

    tasks = [asyncio.create_task(coro()) for coro in
             (_client_to_upstream, _upstream_to_client, _keep_authorized)]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(RuntimeError):
            await client_ws.close()
