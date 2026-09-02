"""The HTTP surface's auth boundary: no valid Supabase JWT, no access.

These tests deliberately avoid the database — they assert that requests are
rejected *before* any handler logic runs, which is the property that matters.
"""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from server.app import app
from server.auth import _decode
from shared.config import settings

SECRET = "test-jwt-secret-at-least-32-characters-long"
USER = "11111111-1111-1111-1111-111111111111"
TEAM = "22222222-2222-2222-2222-222222222222"

# Supabase issues ES256 user tokens signed by a key published via JWKS. A test
# key pair stands in for the auth server's.
_EC_KEY = ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)
    return TestClient(app)


@pytest.fixture
def es256_client(monkeypatch):
    """A client whose JWKS lookup resolves to the test key pair."""
    monkeypatch.setattr(settings, "supabase_url", "http://auth.test")

    class _Key:
        key = _EC_KEY.public_key()

    class _Client:
        def get_signing_key_from_jwt(self, _token):
            return _Key()

    monkeypatch.setattr("server.auth._jwk_client", lambda: _Client())
    return TestClient(app)


def _es256_token(**overrides):
    claims = {"sub": USER, "aud": "authenticated"} | overrides
    return jwt.encode(claims, _EC_KEY, algorithm="ES256")


def _token(secret=SECRET, **overrides):
    claims = {"sub": USER, "aud": "authenticated"} | overrides
    return jwt.encode(claims, secret, algorithm="HS256")


def _turn(client, headers=None):
    return client.post(
        "/agent/turn",
        json={"team_id": TEAM, "text": "hello", "thread_type": "private"},
        headers=headers or {},
    )


def test_health_needs_no_token(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_turn_without_token_is_rejected(client):
    assert _turn(client).status_code == 401


def test_turn_with_malformed_token_is_rejected(client):
    resp = _turn(client, {"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401


def test_token_signed_with_another_secret_is_rejected(client):
    resp = _turn(client, {"Authorization": f"Bearer {_token(secret='wrong-secret')}"})
    assert resp.status_code == 401


def test_token_for_another_audience_is_rejected(client):
    resp = _turn(client, {"Authorization": f"Bearer {_token(aud='anon')}"})
    assert resp.status_code == 401


def test_token_without_subject_is_rejected(client):
    token = jwt.encode({"aud": "authenticated"}, SECRET, algorithm="HS256")
    resp = _turn(client, {"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_unconfigured_secret_fails_closed(monkeypatch):
    """A missing secret must refuse everyone, not fall back to trusting callers."""
    monkeypatch.setattr(settings, "supabase_jwt_secret", "")
    resp = _turn(TestClient(app, raise_server_exceptions=False),
                 {"Authorization": f"Bearer {_token()}"})
    assert resp.status_code == 500


def test_requester_id_in_body_is_ignored(client):
    """The old dev contract let the body claim an identity. It must not work."""
    resp = client.post(
        "/agent/turn",
        json={"team_id": TEAM, "text": "hi", "requester_id": USER},
    )
    assert resp.status_code == 401


# ---------- asymmetric (ES256) tokens: what Supabase actually issues ----------

def test_es256_token_is_accepted(es256_client, monkeypatch):
    """Regression: real Supabase tokens are ES256, not HS256.

    An HS256-only verifier rejects every genuine browser session with
    "The specified alg value is not allowed" — the whole API is unreachable.
    """
    monkeypatch.setattr("server.app.require_membership", lambda *_: None)
    monkeypatch.setattr(
        "server.app.run_turn_sync",
        lambda *a, **k: {"run_id": "r", "reply": "ok", "steps": []},
    )
    monkeypatch.setattr("server.app._persist_user_message", lambda *a: "m1")
    monkeypatch.setattr("server.app._persist_ai_reply", lambda *a: "m2")
    resp = _turn(es256_client, {"Authorization": f"Bearer {_es256_token()}"})
    assert resp.status_code == 200


def test_es256_token_signed_by_another_key_is_rejected(es256_client):
    other = ec.generate_private_key(ec.SECP256R1())
    token = jwt.encode({"sub": USER, "aud": "authenticated"}, other,
                       algorithm="ES256")
    resp = _turn(es256_client, {"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_es256_path_does_not_accept_alg_none(es256_client):
    """Algorithm confusion: a token must not talk the verifier into a weaker scheme."""
    token = jwt.encode({"sub": USER, "aud": "authenticated"}, key="",
                       algorithm="none")
    resp = _turn(es256_client, {"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_cors_allows_the_frontend_origin(client):
    origin = settings.cors_origin_list[0]
    resp = client.options(
        "/agent/turn",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert resp.headers.get("access-control-allow-origin") == origin


def _skewed(claims: dict) -> str:
    """A token whose time claims we control.

    NOT named _token: this file already has one, and appending a second
    definition silently replaced it — three unrelated tests started failing on
    a helper they had used correctly for months. A shadowed name is the
    quietest way to break a test file.
    """
    return jwt.encode(
        {"sub": "u1", "aud": "authenticated", **claims},
        settings.supabase_jwt_secret, algorithm="HS256",
    )


def test_a_token_from_a_slightly_fast_clock_is_accepted(monkeypatch):
    """🔴 An intermittent 401 nobody can diagnose from its message.

    The auth server and the API are different machines, so one of them is
    always a little ahead. Without leeway the API rejects a perfectly good
    token with "The token is not yet valid (iat)" — which the frontend renders
    as "your session expired", so the user signs in again and is handed
    another token from the same fast clock.

    Found by a real agent turn returning 401 between the API and the Supabase
    container on one laptop.
    """
    monkeypatch.setattr("shared.config.settings.supabase_jwt_secret", "s" * 40)
    now = int(time.time())
    claims = _decode(_skewed({"iat": now + 20, "exp": now + 3600}))
    assert claims["sub"] == "u1"


def test_a_token_from_the_distant_future_is_still_refused(monkeypatch):
    """Leeway is tolerance for skew, not for forgery. A token issued an hour
    from now is not a clock being a second fast."""
    monkeypatch.setattr("shared.config.settings.supabase_jwt_secret", "s" * 40)
    now = int(time.time())
    with pytest.raises(jwt.ImmatureSignatureError):
        _decode(_skewed({"iat": now + 3600, "exp": now + 7200}))


def test_a_long_expired_token_is_still_refused(monkeypatch):
    """The leeway applies to exp too, so this pins the boundary: a minute of
    grace, not an open door."""
    monkeypatch.setattr("shared.config.settings.supabase_jwt_secret", "s" * 40)
    now = int(time.time())
    with pytest.raises(jwt.ExpiredSignatureError):
        _decode(_skewed({"iat": now - 7200, "exp": now - 600}))
