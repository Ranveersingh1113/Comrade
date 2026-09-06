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

import jwt

from shared.config import settings
from shared.db import Role, team_session, user_session

#: Short. Long enough to open a link and click around, short enough that a
#: leaked URL in a screenshot is not a standing grant. The re-check on every
#: connection is what actually bounds access; this bounds the blast radius of
#: the token itself.
TOKEN_TTL_SECONDS = 15 * 60

#: A distinct audience, so a preview token cannot be presented to the API as a
#: session token or the other way round. Same secret, different purpose, and
#: without this the difference would rest on which fields happen to be present.
AUDIENCE = "comrade-preview"

#: Headers that must not reach the team's own code. `authorization` and
#: `cookie` carry the member's Supabase session — handing those to an
#: unreviewed server is handing it the member's account. The rest are
#: hop-by-hop and forwarding them through a proxy is a protocol error.
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
    reimplementing the same rule here in Python.
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


def mint(user_id: str, team_id: str, process_id: str) -> str:
    """A short-lived token for one person, one process, one port."""
    row = _process_row(user_id, team_id, process_id)
    if row["state"] not in ("starting", "running"):
        raise PreviewDenied("that preview is not available.")
    if not row["port"]:
        raise PreviewDenied(
            "this process did not declare a port, so it has nothing to preview."
        )
    now = int(time.time())
    return jwt.encode(
        {
            "sub": user_id,
            "team_id": team_id,
            "thread_id": row["thread_id"],
            "process_id": row["process_id"],
            "port": row["port"],
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + TOKEN_TTL_SECONDS,
        },
        settings.supabase_jwt_secret,
        algorithm="HS256",
    )


def verify(token: str) -> dict:
    """Decode and check the signature, audience and expiry. Nothing else."""
    try:
        return jwt.decode(
            token, settings.supabase_jwt_secret,
            algorithms=["HS256"], audience=AUDIENCE,
        )
    except jwt.PyJWTError as exc:
        raise PreviewDenied("that preview link is no longer valid.") from exc


def authorize(token: str) -> dict:
    """Verify the token AND re-ask the database whether it still holds.

    This is the function the proxy calls per connection. See rule 2 in the
    module header: a token that was valid when it was minted says nothing
    about whether its holder is still in the thread.
    """
    claims = verify(token)
    row = _process_row(
        claims["sub"], claims["team_id"], claims["process_id"],
    )
    if row["state"] not in ("starting", "running"):
        raise PreviewDenied("that preview is not available.")
    if not row["container_name"] or not row["port"]:
        raise PreviewDenied("that preview is not available.")
    # The ROW's port, not the token's. If they ever disagree the row is right,
    # and a token that outlived a change must not reach a port nobody approved.
    return {
        "user_id": claims["sub"], "team_id": claims["team_id"],
        "thread_id": row["thread_id"], "process_id": row["process_id"],
        "container_name": row["container_name"], "port": row["port"],
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
