"""HTTP layer for the agent runtime (orchestrator + DB stubbed — no LLM, no DB).

Token verification itself is covered in test_server_auth.py; here the identity
dependency is overridden so these tests exercise handler behaviour instead.
"""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from server.app import app
from server.auth import current_user_id

USER = "11111111-1111-1111-1111-111111111111"
TEAM = "22222222-2222-2222-2222-222222222222"
GROUP_THREAD = "44444444-4444-4444-4444-444444444444"


@pytest.fixture
def client(monkeypatch):
    # Both guards are stubbed: these tests exercise handler behaviour with no
    # DB. Membership is covered in test_server_auth, atomic quota accounting
    # in test_usage_reservations.
    monkeypatch.setattr("server.app.require_membership", lambda *_: None)
    monkeypatch.setattr("server.app.reserve_turn", lambda *_: 0)
    monkeypatch.setattr("server.app.record_reservation", lambda *_: None)
    monkeypatch.setattr("server.app._resolve_thread", lambda _u, _t, thread: str(thread))
    monkeypatch.setattr("server.app.enqueue_turn", lambda *_, **__: "run-1")
    app.dependency_overrides[current_user_id] = lambda: USER
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_health_ok():
    """Healthy now means reachable AND able to see the database — it used to
    mean only that a process was listening, which it reported cheerfully while
    every real request failed on a dead connection pool."""
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok"}


def test_agent_turn_returns_a_durable_run_id(client):
    resp = client.post("/agent/turn", json={"team_id": TEAM, "thread_id": GROUP_THREAD, "text": "status?"})
    assert resp.status_code == 200
    assert resp.json() == {"run_id": "run-1", "status": "queued"}


def test_turn_identity_comes_from_the_token_not_the_body(client, monkeypatch):
    """A body-supplied requester_id must not influence who the agent runs as."""
    seen = {}
    monkeypatch.setattr(
        "server.app.enqueue_turn",
        lambda team_id, requester_id, *_, **__: seen.update(requester=requester_id) or "r",
    )
    resp = client.post(
        "/agent/turn",
        json={"team_id": TEAM, "thread_id": GROUP_THREAD, "text": "hi", "requester_id": "someone-else"},
    )
    assert resp.status_code == 200
    assert seen["requester"] == USER


def test_queue_is_told_the_canonical_thread(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "server.app.enqueue_turn",
        lambda team, user, thread, text, **__: seen.update(
            team=team, user=user, thread=thread, text=text
        ) or "r",
    )
    client.post(
        "/agent/turn",
        json={"team_id": TEAM, "text": "hi", "thread_id": GROUP_THREAD},
    )
    assert seen == {"team": TEAM, "user": USER, "thread": GROUP_THREAD, "text": "hi"}


def test_inaccessible_thread_is_rejected_before_enqueueing(client, monkeypatch):
    monkeypatch.setattr(
        "server.app._resolve_thread",
        lambda *_: (_ for _ in ()).throw(HTTPException(status_code=404)),
    )
    monkeypatch.setattr("server.app.enqueue_turn", lambda *_, **__: pytest.fail("should not enqueue"))

    resp = client.post(
        "/agent/turn", json={"team_id": TEAM, "thread_id": GROUP_THREAD, "text": "hi"}
    )

    assert resp.status_code == 404


def test_consent_approve_passes_the_caller_as_approver(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "server.app.approve_consent",
        lambda team_id, consent_id, approver_id: seen.update(
            approver=approver_id, consent=consent_id
        ) or {"status": "executed", "result": {}},
    )
    resp = client.post("/consent/c-1/approve", json={"team_id": TEAM})
    assert resp.status_code == 200
    assert seen == {"approver": USER, "consent": "c-1"}


def test_consent_approve_can_request_a_thread_grant(client, monkeypatch):
    seen = {}
    monkeypatch.setattr("server.app.require_membership", lambda *_: None)
    monkeypatch.setattr(
        "server.app.approve_consent",
        lambda team, consent, approver, *, grant_for_thread=False: seen.update(
            team=team, consent=consent, approver=approver, grant=grant_for_thread,
        ) or {"status": "executed", "result": {}},
    )

    resp = client.post(
        "/consent/c-1/approve",
        json={"team_id": TEAM, "grant_for_thread": True},
    )

    assert resp.status_code == 200
    assert seen == {"team": TEAM, "consent": "c-1", "approver": USER, "grant": True}


def test_permission_grant_revoke_uses_current_member(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "server.app.revoke_permission_grant",
        lambda team, grant, requester: seen.update(
            team=team, grant=grant, requester=requester,
        ) or True,
    )

    resp = client.post("/permission-grants/g-1/revoke", json={"team_id": TEAM})

    assert resp.status_code == 200
    assert seen == {"team": TEAM, "grant": "g-1", "requester": USER}


def test_consent_not_yours_is_404(client, monkeypatch):
    monkeypatch.setattr(
        "server.app.approve_consent",
        lambda *a: {"status": "not_approved", "reason": "not pending or not yours"},
    )
    resp = client.post("/consent/c-1/approve", json={"team_id": TEAM})
    assert resp.status_code == 404


def test_consent_reject_passes_the_optional_reason_through(client, monkeypatch):
    seen = {}

    def _reject(team_id, consent_id, approver_id, reason):
        seen.update(
            team_id=team_id, consent=consent_id, approver=approver_id, reason=reason
        )
        return {"status": "rejected"}

    monkeypatch.setattr("server.app.reject_consent", _reject)
    resp = client.post(
        "/consent/c-1/reject",
        json={"team_id": TEAM, "reason": "we already decided this in standup"},
    )
    assert resp.status_code == 200
    assert seen == {
        "team_id": TEAM,
        "consent": "c-1",
        "approver": USER,
        "reason": "we already decided this in standup",
    }


def test_consent_reject_defaults_the_reason_to_none(client, monkeypatch):
    """An existing caller that never sends `reason` must keep working."""
    seen = {}

    def _reject(team_id, consent_id, approver_id, reason):
        seen["reason"] = reason
        return {"status": "rejected"}

    monkeypatch.setattr("server.app.reject_consent", _reject)
    resp = client.post("/consent/c-1/reject", json={"team_id": TEAM})
    assert resp.status_code == 200
    assert seen == {"reason": None}


def test_stale_consent_surfaces_as_conflict(client, monkeypatch):
    from shared.consent import ConsentError

    def _raise(*a):
        raise ConsentError("consent c-1 has expired")

    monkeypatch.setattr("server.app.approve_consent", _raise)
    resp = client.post("/consent/c-1/approve", json={"team_id": TEAM})
    assert resp.status_code == 409


def test_suppress_rejects_an_unknown_kind(client):
    """A kind no producer will ever emit is a typo, not a preference (§2.5)."""
    resp = client.post(
        "/observations/00000000-0000-0000-0000-000000000001/suppress",
        json={"team_id": TEAM, "kind": "not_a_real_kind"},
    )
    assert resp.status_code == 422


def test_suppress_accepts_the_known_kind(client):
    """The known kind passes validation; only the DB lookup can reject it.

    404 (no such observation) proves the request body was accepted and the
    handler ran. A 422 would mean validation wrongly rejected a real kind.
    """
    resp = client.post(
        "/observations/00000000-0000-0000-0000-000000000001/suppress",
        json={"team_id": TEAM, "kind": "proactive_observation"},
    )
    assert resp.status_code != 422
