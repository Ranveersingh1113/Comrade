"""fix.md F59 — the deployment said it was ready with a dead Supabase key.

🔴 THE DEFECT, measured on the pilot host. `SUPABASE_SECRET_KEY` was rejected
by every plane of its own project — `sb_secret_…`, correct format, no stray
whitespace, and the project's own `/rest/v1/` replied "Only secret API keys can
be used for this endpoint" when handed the publishable one, so the format was
right and that particular secret simply was not valid there:

    anon   /auth/v1/settings   200      secret /auth/v1/settings   401
    anon   /storage/v1/bucket  200      secret /storage/v1/bucket  400

`/ready` reported all eight checks ok throughout, because nothing on the turn
path touches that API. What it breaks is inviting a member and reading an
uploaded document back out of Storage — and the only way to discover either was
for someone to try.

No database and no network: every test here drives the real function with a
doubled `httpx`, which is the whole point — the defect is in what the code does
with an HTTP status, not in anything Postgres knows.
"""
import pytest

from server import app as server_app
from server import invites
from shared import storage


class _Reply:
    """Just enough of an httpx response for the code under test."""

    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text

    def json(self):
        import json

        return json.loads(self.text)

    def read(self):
        return self.text.encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(
                "raise_for_status ran; the explicit status check should have"
                " raised a named error before this"
            )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# The readiness verdict
# ---------------------------------------------------------------------------


def test_a_rejected_key_is_reported_and_names_both_planes(monkeypatch):
    """Both planes, because they validate separately and failed differently:
    auth said `401 Invalid API key` while storage said `Invalid Compact JWS`,
    same key, same second. One probe would report half the story."""
    monkeypatch.setattr(server_app.settings, "supabase_url",
                        "https://project.supabase.co", raising=False)
    monkeypatch.setattr(server_app.settings, "supabase_secret_key",
                        "sb_secret_dead", raising=False)
    seen = []

    def refuse(url, **kwargs):
        seen.append(url)
        return _Reply(401 if "/auth/" in url else 400)

    monkeypatch.setattr(server_app.httpx, "get", refuse)

    verdict = server_app._supabase_check()

    assert verdict != "ok"
    assert "auth: HTTP 401" in verdict, verdict
    assert "storage: HTTP 400" in verdict, verdict
    # The operator has to be told what still works, or a red line reads as an
    # outage — and agent turns genuinely never touch this API.
    assert "agent turns are unaffected" in verdict, verdict
    assert any("/auth/v1/admin/users" in url for url in seen), seen
    assert any("/storage/v1/bucket" in url for url in seen), seen


def test_a_working_key_is_ok(monkeypatch):
    monkeypatch.setattr(server_app.settings, "supabase_url",
                        "https://project.supabase.co", raising=False)
    monkeypatch.setattr(server_app.settings, "supabase_secret_key",
                        "sb_secret_live", raising=False)
    monkeypatch.setattr(server_app.httpx, "get",
                        lambda url, **kw: _Reply(200, "[]"))

    assert server_app._supabase_check() == "ok"


def test_one_dead_plane_is_still_a_failure(monkeypatch):
    """Storage alone failing still breaks document ingestion. A check that
    passes because the OTHER plane answered is the loose-assertion trap."""
    monkeypatch.setattr(server_app.settings, "supabase_url",
                        "https://project.supabase.co", raising=False)
    monkeypatch.setattr(server_app.settings, "supabase_secret_key",
                        "sb_secret_half", raising=False)
    monkeypatch.setattr(
        server_app.httpx, "get",
        lambda url, **kw: _Reply(200, "[]") if "/auth/" in url else _Reply(403))

    verdict = server_app._supabase_check()

    assert "storage: HTTP 403" in verdict, verdict
    assert "auth" not in verdict, verdict


def test_an_unreachable_project_is_reported_not_raised(monkeypatch):
    """A readiness probe must not become the outage it is reporting on."""
    monkeypatch.setattr(server_app.settings, "supabase_url",
                        "https://project.supabase.co", raising=False)
    monkeypatch.setattr(server_app.settings, "supabase_secret_key",
                        "sb_secret_live", raising=False)

    def explode(url, **kwargs):
        raise OSError("name resolution failed")

    monkeypatch.setattr(server_app.httpx, "get", explode)

    verdict = server_app._supabase_check()

    assert verdict != "ok"
    assert "auth" in verdict and "storage" in verdict, verdict


def test_no_key_configured_says_so_rather_than_calling_out(monkeypatch):
    monkeypatch.setattr(server_app.settings, "supabase_url", "", raising=False)
    monkeypatch.setattr(server_app.settings, "supabase_secret_key", "",
                        raising=False)

    def forbidden(*a, **k):
        raise AssertionError("asked the network with nothing configured")

    monkeypatch.setattr(server_app.httpx, "get", forbidden)

    assert server_app._supabase_check() == "not configured"


def test_the_supabase_verdict_is_reported_without_failing_readiness():
    """The policy, stated as a test rather than as a comment.

    Every other check in that endpoint is fatal on purpose. This one is not,
    because a deployment with a dead key still answers questions — failing
    readiness would take a working pilot offline and block every release. To
    make it fatal, remove the name from ADVISORY_CHECKS; this test is what
    would then have to change with it.
    """
    assert "supabase_api" in server_app.ADVISORY_CHECKS

    healthy = {"database": "ok", "workers": "ok"}
    assert server_app._readiness_ok(healthy) is True
    assert server_app._readiness_ok(
        {**healthy, "supabase_api": "auth: HTTP 401"}) is True
    # ...and nothing else gets that treatment.
    assert server_app._readiness_ok({**healthy, "workers": "none seen"}) is False


# ---------------------------------------------------------------------------
# The two features that actually use the key
# ---------------------------------------------------------------------------


def test_an_invite_names_the_rejected_key(monkeypatch):
    """🔴 It used to answer `502 invite failed: 401`, which is
    indistinguishable from a Supabase outage — and the 401 body contains
    neither "already" nor "exists", so it fell past the registered-user
    heuristic into that bare message."""
    monkeypatch.setattr(invites.settings, "supabase_secret_key",
                        "sb_secret_dead", raising=False)
    monkeypatch.setattr(invites.settings, "supabase_url",
                        "https://project.supabase.co", raising=False)
    monkeypatch.setattr(
        invites.httpx, "post",
        lambda *a, **k: _Reply(401, '{"message":"Invalid API key","hint":'
                                    '"This API key might also be owned by'
                                    ' another Supabase project."}'))

    with pytest.raises(invites.HTTPException) as refused:
        invites._invite_or_resolve_user("someone@example.test")

    assert refused.value.status_code == 503, refused.value.status_code
    detail = refused.value.detail
    assert "SUPABASE_SECRET_KEY" in detail, detail
    assert "supabase_api" in detail, detail


def test_reading_a_document_names_the_rejected_key(monkeypatch):
    """The worker log showed an unlabelled 401 against a URL containing the
    object path, which reads like the document's fault. It is the
    deployment's."""
    monkeypatch.setattr(storage, "assert_object_is_authentic",
                        lambda path, **kw: path)
    monkeypatch.setattr(storage.settings, "supabase_url",
                        "https://project.supabase.co", raising=False)
    monkeypatch.setattr(storage.settings, "supabase_secret_key",
                        "sb_secret_dead", raising=False)
    monkeypatch.setattr(storage.httpx, "stream",
                        lambda *a, **k: _Reply(401))

    with pytest.raises(RuntimeError) as refused:
        storage.download_document(
            "team/doc/file.txt", team_id="team", document_id="doc")

    message = str(refused.value)
    assert "SUPABASE_SECRET_KEY" in message, message
    # Not a DocumentTooLarge or ObjectNotOwned: those are permanent and would
    # fail the document forever. This one stays retryable — which is not the
    # same as self-healing, and the comment beside the raise says so: the
    # worker parks the job after three attempts, minutes before anyone has
    # configured a new key, and the requeue is a deliberate step.
    assert not isinstance(refused.value, storage.ObjectNotOwned)
    assert not isinstance(refused.value, storage.DocumentTooLarge)


def test_a_sick_auth_service_is_not_reported_as_a_rejected_key(monkeypatch):
    """🔴 Found by running the check against a HEALTHY local stack, which is
    what a check has to be tried against before it is trusted.

    The local GoTrue answers `500 Database error finding user` to
    `/auth/v1/admin/users` with a perfectly valid key. The first version of
    this check reported any status >= 400 as the key being rejected, so a
    healthy deployment read as a credential failure — and the remedy it points
    at (issue a new secret key) is not the remedy.
    """
    monkeypatch.setattr(server_app.settings, "supabase_url",
                        "https://project.supabase.co", raising=False)
    monkeypatch.setattr(server_app.settings, "supabase_secret_key",
                        "sb_secret_live", raising=False)
    monkeypatch.setattr(
        server_app.httpx, "get",
        lambda url, **kw: _Reply(500) if "/auth/" in url else _Reply(200, "[]"))

    verdict = server_app._supabase_check()

    assert verdict != "ok", "a 500 is still worth reporting"
    assert "auth: HTTP 500" in verdict, verdict
    assert "key rejected" not in verdict, verdict
    assert "not about the key" in verdict, verdict


def test_each_plane_rejects_with_its_own_status(monkeypatch):
    """Measured on the pilot, same key at the same second: auth answered
    `401 Invalid API key` and storage answered `400 Invalid Compact JWS`. A
    single >= 400 rule cannot tell either of those from an unwell service, and
    one shared rejection list would call a storage 400 healthy."""
    monkeypatch.setattr(server_app.settings, "supabase_url",
                        "https://project.supabase.co", raising=False)
    monkeypatch.setattr(server_app.settings, "supabase_secret_key",
                        "sb_secret_dead", raising=False)
    monkeypatch.setattr(
        server_app.httpx, "get",
        lambda url, **kw: _Reply(401) if "/auth/" in url else _Reply(400))

    verdict = server_app._supabase_check()

    assert "auth: HTTP 401, key rejected" in verdict, verdict
    assert "storage: HTTP 400, key rejected" in verdict, verdict


def test_a_storage_400_is_read_before_it_is_blamed_on_the_key(monkeypatch):
    """🔴 The guard listed 401/403 only, and the measured storage rejection is
    a **400** (`Invalid Compact JWS`, same key, same second as auth's 401). So
    it did not fire on the shape the review actually measured.

    400 alone cannot be the rule either: the local stack answers 400 with
    `Object not found` for a document that is genuinely missing, and calling
    that a credential failure sends an operator to reissue a working key. The
    body is the discriminator, both directions pinned here.
    """
    monkeypatch.setattr(storage, "assert_object_is_authentic",
                        lambda path, **kw: path)
    monkeypatch.setattr(storage.settings, "supabase_url",
                        "https://project.supabase.co", raising=False)
    monkeypatch.setattr(storage.settings, "supabase_secret_key",
                        "sb_secret_dead", raising=False)

    monkeypatch.setattr(
        storage.httpx, "stream",
        lambda *a, **k: _Reply(400, '{"message":"Invalid Compact JWS"}'))
    with pytest.raises(RuntimeError) as refused:
        storage.download_document(
            "team/doc/file.txt", team_id="team", document_id="doc")
    assert "SUPABASE_SECRET_KEY" in str(refused.value), refused.value

    # A missing object is NOT a rejected key. It falls through to
    # raise_for_status, which the double reports as its own kind of failure.
    monkeypatch.setattr(
        storage.httpx, "stream",
        lambda *a, **k: _Reply(400, '{"error":"not_found",'
                                    '"message":"Object not found"}'))
    with pytest.raises(AssertionError) as fell_through:
        storage.download_document(
            "team/doc/file.txt", team_id="team", document_id="doc")
    assert "raise_for_status ran" in str(fell_through.value)
