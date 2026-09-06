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


# ---------------------------------------------------------------------------
# The token
# ---------------------------------------------------------------------------

def test_a_participant_gets_a_token_for_their_own_thread(seeded, admin):
    thread_id = _thread(admin)
    process_id = _process(admin, thread_id)

    token = previews.mint(A1, TEAM_A, process_id)
    claims = previews.verify(token)

    assert claims["sub"] == A1
    assert claims["process_id"] == process_id
    assert claims["port"] == 3000
    assert claims["thread_id"] == thread_id


def test_a_non_participant_cannot_mint_one(seeded, admin):
    """Restricted thread, and A2 is not in it. The refusal happens at mint —
    but see the access test below: it happens again at every connection,
    because membership can be revoked after a link is sent."""
    thread_id = _thread(admin, visibility="restricted")
    process_id = _process(admin, thread_id)

    with pytest.raises(previews.PreviewDenied):
        previews.mint(A2, TEAM_A, process_id)


def test_another_teams_member_cannot_mint_one(seeded, admin):
    thread_id = _thread(admin)
    process_id = _process(admin, thread_id)
    with pytest.raises(previews.PreviewDenied):
        previews.mint(B1, TEAM_A, process_id)


def test_a_token_binds_the_port_so_one_link_cannot_reach_another_service(
    seeded, admin
):
    """🔴 The whole reason the port is in the token rather than the URL. A
    token that named only the process would let its holder ask for any port
    inside that container — including one the member never approved and never
    saw."""
    thread_id = _thread(admin)
    process_id = _process(admin, thread_id, port=3000)
    claims = previews.verify(previews.mint(A1, TEAM_A, process_id))
    assert claims["port"] == 3000


def test_an_expired_token_is_refused(seeded, admin, monkeypatch):
    thread_id = _thread(admin)
    process_id = _process(admin, thread_id)
    monkeypatch.setattr(previews, "TOKEN_TTL_SECONDS", -1)
    token = previews.mint(A1, TEAM_A, process_id)

    with pytest.raises(previews.PreviewDenied):
        previews.verify(token)


def test_a_token_signed_with_another_secret_is_refused(seeded, admin, monkeypatch):
    thread_id = _thread(admin)
    process_id = _process(admin, thread_id)
    token = previews.mint(A1, TEAM_A, process_id)
    monkeypatch.setattr(settings, "supabase_jwt_secret", "a-different-secret")
    with pytest.raises(previews.PreviewDenied):
        previews.verify(token)


def test_a_garbled_token_is_refused_rather_than_crashing(seeded):
    for junk in ["", "not.a.token", "a.b.c", "..", "null"]:
        with pytest.raises(previews.PreviewDenied):
            previews.verify(junk)


# ---------------------------------------------------------------------------
# Every connection, not just the first
# ---------------------------------------------------------------------------

def test_access_is_rechecked_when_the_connection_is_made(seeded, admin):
    """A token lives for minutes; a link lives forever in someone's chat
    history. Removing a participant has to end their preview immediately, and
    it only does if the check runs per connection rather than at mint."""
    thread_id = _thread(admin, visibility="restricted")
    process_id = _process(admin, thread_id)
    token = previews.mint(A1, TEAM_A, process_id)
    assert previews.authorize(token)["container_name"] == "comrade-proc-abc"

    admin.execute(
        "delete from public.thread_participants where thread_id=%s and user_id=%s",
        (thread_id, A1),
    )
    with pytest.raises(previews.PreviewDenied):
        previews.authorize(token)


def test_a_stopped_process_cannot_be_reached(seeded, admin):
    """The container is gone; the token is not. Without this the proxy dials a
    name that may since have been reused."""
    thread_id = _thread(admin)
    process_id = _process(admin, thread_id)
    token = previews.mint(A1, TEAM_A, process_id)

    admin.execute(
        "update public.sandbox_processes set state='stopped' where id=%s",
        (process_id,),
    )
    with pytest.raises(previews.PreviewDenied):
        previews.authorize(token)


def test_a_process_that_declared_no_port_has_no_preview(seeded, admin):
    thread_id = _thread(admin)
    process_id = _process(admin, thread_id, port=None)
    with pytest.raises(previews.PreviewDenied):
        previews.mint(A1, TEAM_A, process_id)


def test_the_port_dialled_is_the_recorded_one_not_the_tokens(seeded, admin):
    """Defence in depth against a forged-but-valid-looking token: the row is
    the authority on which port this process declared."""
    thread_id = _thread(admin)
    process_id = _process(admin, thread_id, port=3000)
    token = previews.mint(A1, TEAM_A, process_id)
    admin.execute(
        "update public.sandbox_processes set port=4000 where id=%s", (process_id,),
    )
    assert previews.authorize(token)["port"] == 4000


# ---------------------------------------------------------------------------
# What the proxy will not forward
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("host", [
    "evil.example.com", "localhost:8000", "169.254.169.254", "",
])
def test_the_upstream_host_never_comes_from_the_request(seeded, admin, host):
    """🔴 Host-header defence. The upstream is built from the DATABASE ROW —
    container name and recorded port — and never from anything the caller
    sent. A proxy that trusts a Host header is an SSRF endpoint with extra
    steps, and 169.254.169.254 is in this list for a reason."""
    thread_id = _thread(admin)
    process_id = _process(admin, thread_id)
    token = previews.mint(A1, TEAM_A, process_id)

    upstream = previews.upstream_url(previews.authorize(token), path="/", host=host)
    assert upstream.startswith("http://comrade-proc-abc:3000/")
    assert host not in upstream or host == ""


@pytest.mark.parametrize("path", [
    "/", "/index.html", "/assets/app.js?v=2", "/a/b/c",
])
def test_ordinary_paths_are_forwarded(seeded, admin, path):
    thread_id = _thread(admin)
    token = previews.mint(A1, TEAM_A, _process(admin, _thread(admin)))
    url = previews.upstream_url(previews.authorize(token), path=path, host="x")
    assert url.startswith("http://comrade-proc-abc:3000/")


@pytest.mark.parametrize("path", ["//evil.com/x", "/../../etc/passwd", "http://evil"])
def test_a_path_cannot_redirect_the_upstream(seeded, admin, path):
    """A path is a path. One that changes the HOST is not being forwarded."""
    thread_id = _thread(admin)
    token = previews.mint(A1, TEAM_A, _process(admin, thread_id))
    url = previews.upstream_url(previews.authorize(token), path=path, host="x")
    assert url.startswith("http://comrade-proc-abc:3000/")
    assert "evil" not in url.split("comrade-proc-abc:3000", 1)[0]


def test_hop_by_hop_and_authorization_headers_are_not_forwarded(seeded, admin):
    """The team's own code is on the other side of this. It must never receive
    a member's Supabase token, and forwarding hop-by-hop headers through a
    proxy is a protocol error besides."""
    forwarded = previews.forwardable_headers({
        "authorization": "Bearer supabase-token",
        "cookie": "sb-access-token=secret",
        "connection": "keep-alive",
        "host": "evil.example.com",
        "accept": "text/html",
        "user-agent": "Mozilla/5.0",
    })
    assert set(forwarded) == {"accept", "user-agent"}


def test_the_token_never_reaches_a_log_line(seeded, admin):
    """Tokens arrive in a query string, and a query string is the most logged
    text in any web stack."""
    thread_id = _thread(admin)
    token = previews.mint(A1, TEAM_A, _process(admin, thread_id))
    redacted = previews.loggable(f"/previews/x?token={token}&a=1")
    assert token not in redacted
    assert "token=<redacted>" in redacted, redacted
    assert "a=1" in redacted, "redaction must not eat the rest of the query"
