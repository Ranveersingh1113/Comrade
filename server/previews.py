"""Reaching a development server a thread started, and nothing else.

WHAT IS ON THE OTHER SIDE OF THIS. A server written by a model, running a
team's unreviewed code, with no authentication of its own. It is on an internal
Docker network with no route out precisely so that the ONLY way to it is
through here — which makes this module the whole boundary rather than one layer
of it.

THE TWO RULES THAT DO THE WORK

1. The upstream address is built from the DATABASE ROW — container name and
   recorded port — and never from anything the caller sent. A proxy that takes
   its destination from a header is an SSRF endpoint with extra steps, and the
   first thing anyone points one at is 169.254.169.254.

2. Access is re-checked on EVERY connection, not at mint. A token lives for
   minutes; a link lives forever in someone's chat history. Removing a
   participant has to end their preview immediately, and it only does if the
   question is asked again each time.
"""
import re
import time
import uuid as _uuid
from datetime import datetime, timedelta, timezone

import jwt

from shared.config import settings
from shared.db import Role, team_session, user_session

#: What the same-origin scheme used to live here.
#
# 🔴 REMOVED, not deprecated. `mint`/`verify`/`authorize` issued a bearer token
# for `/previews/<id>/` on COMRADE'S OWN ORIGIN, which let a team's unreviewed
# development server read the member's Supabase session out of localStorage.
# The functions are deleted rather than left unused because an unused function
# is one import away from being a route again, and the plan's rollback note for
# this task is explicit: never restore unsafe same-origin execution.
#
# The replacement is below: per-process origins, a single-use launch grant, and
# a host-scoped HttpOnly cookie rechecked against the database on every request.


#: Headers that must not reach the team's own code. `authorization` and
#: `cookie` carry the member's session — handing those to an unreviewed server
#: is handing it the member's account. The rest are hop-by-hop, and forwarding
#: them through a proxy is a protocol error besides.
_STRIPPED = frozenset({
    "authorization", "cookie", "host", "connection", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "transfer-encoding", "upgrade", "content-length",
})


class PreviewDenied(Exception):
    """No preview for this caller, this process, or this moment."""


def _process_row(user_id: str, team_id: str, process_id: str) -> dict:
    """The process, read AS THE MEMBER so RLS answers 'may they see it'.

    Not as the agent role with a team filter: `au_sandbox_processes_select`
    already encodes thread visibility including restricted threads, and asking
    the database the question it was designed to answer is stronger than
    reimplementing the same rule in Python.
    """
    with user_session(user_id) as conn:
        row = conn.execute(
            "select p.id, p.thread_id, p.port, p.state, p.container_name"
            "  from public.sandbox_processes p"
            " where p.id = %s and p.team_id = %s",
            (process_id, team_id),
        ).fetchone()
    if row is None:
        # Deliberately the same message as a stopped process: telling a
        # stranger apart from a participant whose process ended would leak
        # which thread ids exist.
        raise PreviewDenied("that preview is not available.")
    return {
        "process_id": str(row[0]), "thread_id": str(row[1]), "port": row[2],
        "state": row[3], "container_name": row[4],
    }


def upstream_url(grant: dict, *, path: str, host: str = "") -> str:
    """Where to forward. Built from the grant; `host` is accepted only so that
    callers cannot pretend it was used.

    The path is normalised to a single leading slash, which is what stops
    `//evil.com/x` — a protocol-relative URL — from moving the destination.
    """
    safe = "/" + path.lstrip("/")
    safe = re.sub(r"^/+", "/", safe)
    return f"http://{grant['container_name']}:{grant['port']}{safe}"


def forwardable_headers(headers: dict[str, str]) -> dict[str, str]:
    """The request headers that may cross into the team's own code."""
    return {k: v for k, v in headers.items() if k.lower() not in _STRIPPED}


def loggable(target: str) -> str:
    """A request line with the token removed.

    Tokens arrive in a query string, and a query string is the most-logged text
    in any web stack — access logs, error trackers, proxy logs. Redacted at the
    point of formatting so no caller has to remember.
    """
    return re.sub(r"([?&])token=[^&]*", r"\1token=<redacted>", target)


# ---------------------------------------------------------------------------
# Origin isolation (T02)
# ---------------------------------------------------------------------------
#
# 🔴 Everything above this line served previews from Comrade's own hostname.
# That was the defect: a browser's security boundary is the ORIGIN, so a
# development server on the app's origin can read the member's Supabase session
# out of localStorage. Header stripping is irrelevant to it — there was one
# origin, and the team's unreviewed code was inside it.
#
# Each process now answers on its own hostname under a separate domain, which
# is a different site. That costs a credential, because a fresh origin has
# none: the app origin mints a single-use grant, the browser carries it once,
# and the preview origin trades it for a host-scoped HttpOnly cookie.

#: Audiences. A preview session must not be usable as a Comrade session, nor a
#: grant as a session; the audience is what makes that structural rather than a
#: matter of which fields happen to be present.
GRANT_AUDIENCE = "comrade-preview-grant"
SESSION_AUDIENCE = "comrade-preview-session"

#: How long a redeemed preview session lasts. Longer than a grant because it is
#: HttpOnly and host-scoped; shorter than a working day because the per-request
#: recheck below is what really bounds it.
SESSION_TTL_SECONDS = 60 * 60

_HOST_PREFIX = "p-"

#: Response headers a team's own server may not decide for us. It must not set
#: cookies on the preview origin — ours is the only cookie there — and it must
#: not relax the transport security we set on its behalf.
_RESPONSE_DROPPED = frozenset({
    "set-cookie", "strict-transport-security", "content-security-policy",
    "content-security-policy-report-only", "cross-origin-opener-policy",
    "access-control-allow-origin", "access-control-allow-credentials",
})


class PreviewUnconfigured(Exception):
    """Previews are not safely configured, so they are refused."""


def _domain() -> str:
    domain = (settings.comrade_preview_domain or "").strip().lower()
    if not domain:
        raise PreviewUnconfigured(
            "previews are disabled: COMRADE_PREVIEW_DOMAIN is not set. It must"
            " be a domain SEPARATE from the application's, because a preview"
            " sharing Comrade's origin can read a member's session."
        )
    return domain


def assert_origin_isolation() -> None:
    """Refuse a configuration that puts previews back on the app's origin.

    Configuring both the same reintroduces the defect through settings rather
    than through code, which would look like a working deployment.
    """
    domain = _domain()
    app_hosts = {
        origin.split("//")[-1].split("/")[0].split(":")[0].lower()
        for origin in (settings.cors_origins or "").split(",")
        if origin.strip()
    }
    if domain in app_hosts:
        raise PreviewUnconfigured(
            f"COMRADE_PREVIEW_DOMAIN ({domain}) is one of the application's own"
            " origins. Previews must be served from a separate domain."
        )


def host_for(process_id: str) -> str:
    """The hostname this process answers on.

    Derived from the process id rather than stored, so two rows cannot claim
    one hostname and a reload always reaches the same server.
    """
    return f"{_HOST_PREFIX}{_uuid.UUID(str(process_id)).hex}.{_domain()}"


def process_for_host(host: str) -> str | None:
    """Which process a hostname names, or None. Never raises on junk."""
    try:
        domain = _domain()
    except PreviewUnconfigured:
        return None
    host = (host or "").split(":")[0].strip().lower()
    suffix = f".{domain}"
    if not host.endswith(suffix):
        return None
    label = host[: -len(suffix)]
    if not label.startswith(_HOST_PREFIX):
        return None
    try:
        return str(_uuid.UUID(hex=label[len(_HOST_PREFIX):]))
    except ValueError:
        return None


def launch(user_id: str, team_id: str, process_id: str) -> dict:
    """A one-time URL that opens this preview on its own origin."""
    assert_origin_isolation()
    row = _process_row(user_id, team_id, process_id)
    if row["state"] not in ("starting", "running") or not row["port"]:
        raise PreviewDenied("that preview is not available.")

    host = host_for(process_id)
    jti = str(_uuid.uuid4())
    expires = datetime.now(timezone.utc) + timedelta(
        seconds=settings.comrade_preview_grant_seconds
    )
    with team_session(Role.AGENT, team_id) as conn:
        conn.execute(
            "insert into public.preview_grants"
            " (jti, process_id, team_id, user_id, host, expires_at)"
            " values (%s,%s,%s,%s,%s,%s)",
            (jti, process_id, team_id, user_id, host, expires),
        )
    grant = jwt.encode(
        {"jti": jti, "sub": user_id, "team_id": team_id,
         "process_id": process_id, "host": host, "aud": GRANT_AUDIENCE,
         "exp": int(expires.timestamp())},
        settings.supabase_jwt_secret, algorithm="HS256",
    )
    return {"grant": grant, "host": host,
            "url": f"https://{host}/__comrade/launch?grant={grant}"}


def redeem(grant: str, *, host: str) -> dict:
    """Trade a launch grant for the right to set a preview session. Once."""
    try:
        claims = jwt.decode(
            grant, settings.supabase_jwt_secret,
            algorithms=["HS256"], audience=GRANT_AUDIENCE,
        )
    except jwt.PyJWTError as exc:
        raise PreviewDenied("that preview link is no longer valid.") from exc
    if claims.get("host") != host:
        # A grant redeemed on another process's origin would hand one thread's
        # member a session for another thread's server.
        raise PreviewDenied("that preview link is no longer valid.")

    with team_session(Role.AGENT, claims["team_id"]) as conn:
        consumed = conn.execute(
            "update public.preview_grants set consumed_at = now()"
            " where jti=%s and host=%s and consumed_at is null"
            "   and expires_at > now()"
            " returning process_id, user_id",
            (claims["jti"], host),
        ).fetchone()
    if consumed is None:
        # Already used, expired, or never existed — one message for all three,
        # because telling them apart tells an attacker which it was.
        raise PreviewDenied("that preview link is no longer valid.")
    return {"user_id": str(consumed[1]), "team_id": claims["team_id"],
            "process_id": str(consumed[0]), "host": host}


def session_cookie(redeemed: dict) -> dict:
    """The cookie attributes for a redeemed preview.

    NO Domain attribute, deliberately: a wildcard Domain would put one
    preview's cookie on every sibling preview, which is exactly the isolation
    being bought. Host-only means the browser sends it to this hostname alone.
    """
    now = int(time.time())
    value = jwt.encode(
        {"sub": redeemed["user_id"], "team_id": redeemed["team_id"],
         "process_id": redeemed["process_id"], "host": redeemed["host"],
         "aud": SESSION_AUDIENCE, "iat": now, "exp": now + SESSION_TTL_SECONDS},
        settings.supabase_jwt_secret, algorithm="HS256",
    )
    return {
        "key": "comrade_preview", "value": value,
        "max_age": SESSION_TTL_SECONDS, "httponly": True, "secure": True,
        "samesite": "lax", "path": "/",
    }


def authorize_session(value: str, *, host: str) -> dict:
    """Authorize one request on a preview origin.

    Runs on EVERY request, not once at redemption: the cookie outlives the
    grant, and removing a participant has to end their preview immediately.
    """
    try:
        claims = jwt.decode(
            value, settings.supabase_jwt_secret,
            algorithms=["HS256"], audience=SESSION_AUDIENCE,
        )
    except jwt.PyJWTError as exc:
        raise PreviewDenied("that preview is not available.") from exc
    if claims.get("host") != host or process_for_host(host) != claims["process_id"]:
        raise PreviewDenied("that preview is not available.")

    row = _process_row(claims["sub"], claims["team_id"], claims["process_id"])
    if row["state"] not in ("starting", "running"):
        raise PreviewDenied("that preview is not available.")
    if not row["container_name"] or not row["port"]:
        raise PreviewDenied("that preview is not available.")
    return {"user_id": claims["sub"], "team_id": claims["team_id"],
            "thread_id": row["thread_id"], "process_id": row["process_id"],
            "container_name": row["container_name"], "port": row["port"]}


def response_headers(headers: dict[str, str]) -> dict[str, str]:
    """What may come back from a team's own server, plus what we insist on."""
    cleaned = {
        k: v for k, v in headers.items()
        if k.lower() not in _RESPONSE_DROPPED and k.lower() not in _STRIPPED
    }
    # no-store because a preview is unreviewed output that must not be cached
    # by an intermediary; no-referrer so a link clicked inside it does not leak
    # the preview hostname, which names a process id.
    cleaned["cache-control"] = "no-store"
    cleaned["referrer-policy"] = "no-referrer"
    cleaned["x-content-type-options"] = "nosniff"
    return cleaned
