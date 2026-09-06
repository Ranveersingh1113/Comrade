"""A preview must not be able to read the member's Comrade session.

🔴 THE DEFECT THIS CLOSES. Previews were served from Comrade's own hostname at
`/previews/<id>/`. A browser's security boundary is the ORIGIN, so a
development server — written by a model, running a team's unreviewed code —
could read `localStorage` on that origin and take the member's Supabase
session. Stripping headers at the proxy does nothing about this; there was
simply only one origin.

Each process now answers on its own hostname, which is a different site. That
costs a credential, because a fresh origin has none: the app origin mints a
single-use grant, the browser carries it once, and the preview origin exchanges
it for a host-scoped HttpOnly cookie.
"""
import psycopg
import pytest

from server import previews
from shared.config import settings
from tests._seed import A1, A2, TEAM_A

PREVIEW_DOMAIN = "previews.example.test"


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def preview_domain(monkeypatch):
    monkeypatch.setattr(settings, "comrade_preview_domain", PREVIEW_DOMAIN)


def _thread(admin):
    return str(admin.execute(
        "select id from public.threads where team_id=%s and title='General'",
        (TEAM_A,),
    ).fetchone()[0])


def _process(admin, thread_id, port=3000, state="running"):
    return str(admin.execute(
        "insert into public.sandbox_processes"
        " (team_id, thread_id, command, port, container_id, container_name, state)"
        " values (%s,%s,'npm run dev',%s,'abc123','comrade-proc-abc',%s)"
        " returning id",
        (TEAM_A, thread_id, port, state),
    ).fetchone()[0])


# ---------------------------------------------------------------------------
# The origin
# ---------------------------------------------------------------------------

def test_each_process_gets_its_own_hostname(seeded, admin):
    thread_id = _thread(admin)
    one, two = _process(admin, thread_id), _process(admin, thread_id)

    assert previews.host_for(one) != previews.host_for(two)
    assert previews.host_for(one).endswith(f".{PREVIEW_DOMAIN}")


def test_the_hostname_is_derived_from_the_process_identity(seeded, admin):
    """Stable, so a reload reaches the same server, and derived rather than
    stored so two rows cannot claim one hostname."""
    thread_id = _thread(admin)
    process_id = _process(admin, thread_id)
    assert previews.host_for(process_id) == previews.host_for(process_id)
    assert previews.process_for_host(previews.host_for(process_id)) == process_id


def test_a_hostname_on_another_domain_resolves_to_nothing(seeded):
    """The app's own domain most of all: that is the arrangement being
    replaced, and it must not be reachable by spoofing a Host header."""
    for host in ["comrade.example.com", "evil.test", "", "previews.example.test"]:
        assert previews.process_for_host(host) is None


def test_previews_fail_closed_when_no_domain_is_configured(seeded, admin, monkeypatch):
    """🔴 The most important line in this file. An unset preview domain must
    not fall back to serving from the application's origin — the whole defect.
    It refuses, and says why."""
    monkeypatch.setattr(settings, "comrade_preview_domain", "")
    thread_id = _thread(admin)
    process_id = _process(admin, thread_id)

    with pytest.raises(previews.PreviewUnconfigured) as caught:
        previews.launch(A1, TEAM_A, process_id)
    assert "COMRADE_PREVIEW_DOMAIN" in str(caught.value)


def test_a_preview_domain_equal_to_the_app_domain_is_refused(seeded, monkeypatch):
    """Configuring the two the same reintroduces the defect through settings
    rather than through code, and would look like a working deployment."""
    monkeypatch.setattr(settings, "cors_origins", "https://comrade.example.com")
    monkeypatch.setattr(settings, "comrade_preview_domain", "comrade.example.com")
    with pytest.raises(previews.PreviewUnconfigured):
        previews.assert_origin_isolation()


# ---------------------------------------------------------------------------
# The launch grant
# ---------------------------------------------------------------------------

def test_a_launch_grant_names_the_host_it_may_be_redeemed_on(seeded, admin):
    thread_id = _thread(admin)
    process_id = _process(admin, thread_id)
    launch = previews.launch(A1, TEAM_A, process_id)

    assert launch["url"].startswith(f"https://{previews.host_for(process_id)}/")
    assert "grant=" in launch["url"]


def test_a_grant_works_once(seeded, admin):
    """It travels in a URL, and URLs live in history and screenshots."""
    thread_id = _thread(admin)
    process_id = _process(admin, thread_id)
    grant = previews.launch(A1, TEAM_A, process_id)["grant"]
    host = previews.host_for(process_id)

    assert previews.redeem(grant, host=host)["user_id"] == A1
    with pytest.raises(previews.PreviewDenied):
        previews.redeem(grant, host=host)


def test_a_grant_cannot_be_redeemed_on_another_processes_origin(seeded, admin):
    """Otherwise one thread's member trades their grant for a session on
    another thread's server."""
    thread_id = _thread(admin)
    mine, theirs = _process(admin, thread_id), _process(admin, thread_id)
    grant = previews.launch(A1, TEAM_A, mine)["grant"]

    with pytest.raises(previews.PreviewDenied):
        previews.redeem(grant, host=previews.host_for(theirs))


def test_an_expired_grant_is_refused(seeded, admin, monkeypatch):
    thread_id = _thread(admin)
    process_id = _process(admin, thread_id)
    monkeypatch.setattr(settings, "comrade_preview_grant_seconds", -1)
    grant = previews.launch(A1, TEAM_A, process_id)["grant"]

    with pytest.raises(previews.PreviewDenied):
        previews.redeem(grant, host=previews.host_for(process_id))


def test_a_non_participant_cannot_launch(seeded, admin, monkeypatch):
    thread_id = str(admin.execute(
        "select id from public.threads where team_id=%s and visibility='restricted'"
        "  and owner_id=%s limit 1", (TEAM_A, A1),
    ).fetchone()[0])
    process_id = _process(admin, thread_id)
    with pytest.raises(previews.PreviewDenied):
        previews.launch(A2, TEAM_A, process_id)


# ---------------------------------------------------------------------------
# The session cookie
# ---------------------------------------------------------------------------

def test_the_session_cookie_is_host_only_and_not_readable_by_script(seeded, admin):
    """No Domain attribute at all — a wildcard Domain would put one preview's
    cookie on every sibling preview, which is the isolation being bought."""
    thread_id = _thread(admin)
    process_id = _process(admin, thread_id)
    grant = previews.launch(A1, TEAM_A, process_id)["grant"]
    host = previews.host_for(process_id)
    cookie = previews.session_cookie(previews.redeem(grant, host=host))

    assert cookie["httponly"] is True
    assert cookie["secure"] is True
    assert cookie["samesite"] == "lax"
    assert "domain" not in cookie, "a Domain attribute shares this across origins"


def test_a_preview_session_is_not_accepted_by_the_application_api(seeded, admin):
    """Different audience, so a preview session presented to /agent/turn is not
    a Comrade session and vice versa."""
    thread_id = _thread(admin)
    process_id = _process(admin, thread_id)
    grant = previews.launch(A1, TEAM_A, process_id)["grant"]
    host = previews.host_for(process_id)
    value = previews.session_cookie(previews.redeem(grant, host=host))["value"]

    import jwt
    claims = jwt.decode(value, settings.supabase_jwt_secret,
                        algorithms=["HS256"], audience=previews.SESSION_AUDIENCE)
    assert claims["aud"] == previews.SESSION_AUDIENCE
    with pytest.raises(jwt.InvalidAudienceError):
        jwt.decode(value, settings.supabase_jwt_secret,
                   algorithms=["HS256"], audience="authenticated")


def test_a_session_for_one_host_is_refused_on_another(seeded, admin):
    thread_id = _thread(admin)
    mine, theirs = _process(admin, thread_id), _process(admin, thread_id)
    grant = previews.launch(A1, TEAM_A, mine)["grant"]
    value = previews.session_cookie(
        previews.redeem(grant, host=previews.host_for(mine))
    )["value"]

    assert previews.authorize_session(value, host=previews.host_for(mine))
    with pytest.raises(previews.PreviewDenied):
        previews.authorize_session(value, host=previews.host_for(theirs))


def test_access_is_still_rechecked_on_every_request(seeded, admin):
    """The cookie lives longer than a grant. Removing a participant has to end
    their preview immediately, so the database is asked again each time."""
    thread_id = str(admin.execute(
        "select id from public.threads where team_id=%s and visibility='restricted'"
        "  and owner_id=%s limit 1", (TEAM_A, A1),
    ).fetchone()[0])
    process_id = _process(admin, thread_id)
    grant = previews.launch(A1, TEAM_A, process_id)["grant"]
    host = previews.host_for(process_id)
    value = previews.session_cookie(previews.redeem(grant, host=host))["value"]
    assert previews.authorize_session(value, host=host)

    admin.execute(
        "delete from public.thread_participants where thread_id=%s and user_id=%s",
        (thread_id, A1),
    )
    with pytest.raises(previews.PreviewDenied):
        previews.authorize_session(value, host=host)


# ---------------------------------------------------------------------------
# What comes back
# ---------------------------------------------------------------------------

def test_upstream_cookies_and_security_headers_are_dropped(seeded):
    """The team's server must not set cookies on the preview origin — ours is
    the only one there — and must not relax the headers we set on its behalf."""
    cleaned = previews.response_headers({
        "set-cookie": "session=theirs",
        "content-security-policy": "default-src *",
        "strict-transport-security": "max-age=0",
        "content-type": "text/html",
        "cache-control": "public, max-age=31536000",
    })
    assert "set-cookie" not in cleaned
    assert "strict-transport-security" not in cleaned
    assert cleaned["content-type"] == "text/html"
    assert cleaned["cache-control"] == "no-store"
    assert cleaned["referrer-policy"] == "no-referrer"


# ---------------------------------------------------------------------------
# What a preview response may be (T04)
# ---------------------------------------------------------------------------

def test_an_oversized_response_is_refused_not_truncated():
    """🔴 This replaced `upstream.content[:limit]`, which buffered the whole
    response and then sliced it. Slicing does not protect the memory — it is
    already read — and what comes back is half a JavaScript bundle served with
    a 200. Silent corruption is worse than a refusal."""
    with pytest.raises(previews.PreviewTooLarge):
        previews.enforce_response_limit({"content-length": str(999 * 1024 * 1024)},
                                        25 * 1024 * 1024)


def test_an_ordinary_response_passes_the_limit_check():
    previews.enforce_response_limit({"content-length": "1024"}, 25 * 1024 * 1024)
    previews.enforce_response_limit({}, 25 * 1024 * 1024)   # chunked: no length


def test_content_encoding_survives_because_the_body_is_not_decoded():
    """Decoding the body while forwarding `content-encoding: gzip` hands the
    browser a header promising compression over bytes that are not compressed.
    It renders as garbage rather than as an error, which is the worst kind."""
    out = previews.response_headers({
        "content-encoding": "gzip", "content-length": "42",
        "content-type": "application/javascript",
    })
    assert out["content-encoding"] == "gzip"
    assert out["content-length"] == "42"


def test_the_security_headers_we_add_still_win():
    out = previews.response_headers({"cache-control": "public, max-age=31536000"})
    assert out["cache-control"] == "no-store"
    assert out["x-content-type-options"] == "nosniff"
