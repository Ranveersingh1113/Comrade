"""A preview is a hole in every boundary this system has, unless it is not.

The thing being exposed is a development server written by a model, running a
team's unreviewed code, with no authentication of its own. Everything here is
about making sure the only way to reach it is through a link that names exactly
one person, one thread, one process and one port — and that stops working.

The plan's list, in order: participant access, non-participant denial, expiry,
stopped-process denial, host-header defence, arbitrary-port denial, path
proxying, request limits, and no token in the logs.
"""
import time

import psycopg
import pytest

from server import previews
from shared.config import settings
from tests._seed import A1, A2, B1, TEAM_A, TEAM_B


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def _thread(admin, team_id=TEAM_A, visibility="team"):
    if visibility == "team":
        return str(admin.execute(
            "select id from public.threads where team_id=%s and title='General'",
            (team_id,),
        ).fetchone()[0])
    # The seed already owns one restricted thread per member, and
    # uq_threads_personal_discussion enforces that — so reuse it rather than
    # fighting a constraint that is doing its job.
    return str(admin.execute(
        "select id from public.threads where team_id=%s and visibility='restricted'"
        "   and owner_id=%s limit 1",
        (team_id, A1),
    ).fetchone()[0])


def _process(admin, thread_id, team_id=TEAM_A, port=3000, state="running"):
    return str(admin.execute(
        "insert into public.sandbox_processes"
        " (team_id, thread_id, command, port, container_id, container_name, state)"
        " values (%s,%s,'npm run dev',%s,'abc123','comrade-proc-abc',%s)"
        " returning id",
        (team_id, thread_id, port, state),
    ).fetchone()[0])


# The token scheme that used to be tested here — mint/verify/authorize for a
# same-origin `/previews/<id>/` route — is gone with the vulnerability it
# implemented. Its properties (expiry, audience separation, per-connection
# recheck, stopped-process denial) are all re-tested against the replacement in
# tests/test_preview_origins.py, against per-process ORIGINS rather than a
# bearer token on Comrade's own hostname.


# ---------------------------------------------------------------------------
# What the proxy will not forward
# ---------------------------------------------------------------------------
#
# These exercise pure functions, so they build a grant directly rather than
# going through a launch: what is under test is what the proxy will and will
# not forward, not how the grant was obtained.

GRANT = {
    "user_id": A1, "team_id": TEAM_A, "thread_id": "t", "process_id": "p",
    "container_name": "comrade-proc-abc", "port": 3000,
}


@pytest.mark.parametrize("host", [
    "evil.example.com", "localhost:8000", "169.254.169.254", "",
])
def test_the_upstream_host_never_comes_from_the_request(host):
    """🔴 Host-header defence. The upstream is built from the DATABASE ROW —
    container name and recorded port — and never from anything the caller
    sent. A proxy that trusts a Host header is an SSRF endpoint with extra
    steps, and 169.254.169.254 is in this list for a reason."""
    upstream = previews.upstream_url(GRANT, path="/", host=host)
    assert upstream.startswith("http://comrade-proc-abc:3000/")


@pytest.mark.parametrize("path", ["/", "/index.html", "/assets/app.js", "/a/b/c"])
def test_ordinary_paths_are_forwarded(path):
    url = previews.upstream_url(GRANT, path=path, host="x")
    assert url.startswith("http://comrade-proc-abc:3000/")


@pytest.mark.parametrize("path", ["//evil.com/x", "/../../etc/passwd", "http://evil"])
def test_a_path_cannot_redirect_the_upstream(path):
    """A path is a path. One that changes the HOST is not being forwarded —
    `//evil.com/x` is a protocol-relative URL, not a path."""
    url = previews.upstream_url(GRANT, path=path, host="x")
    assert url.startswith("http://comrade-proc-abc:3000/")
    assert "evil" not in url.split("comrade-proc-abc:3000", 1)[0]


def test_hop_by_hop_and_authorization_headers_are_not_forwarded():
    """The team's own code is on the other side of this. It must never receive
    a member's session, and forwarding hop-by-hop headers through a proxy is a
    protocol error besides."""
    forwarded = previews.forwardable_headers({
        "authorization": "Bearer supabase-token",
        "cookie": "comrade_preview=secret",
        "connection": "keep-alive",
        "host": "evil.example.com",
        "accept": "text/html",
        "user-agent": "Mozilla/5.0",
    })
    assert set(forwarded) == {"accept", "user-agent"}


def test_a_credential_in_a_query_string_never_reaches_a_log_line():
    """Query strings are the most-logged text in any web stack, and the launch
    grant travels in one."""
    redacted = previews.loggable("/__comrade/launch?token=abc.def.ghi&a=1")
    assert "abc.def.ghi" not in redacted
    assert "token=<redacted>" in redacted
    assert "a=1" in redacted, "redaction must not eat the rest of the query"
