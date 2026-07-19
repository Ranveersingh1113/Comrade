"""HTTP layer for the agent runtime (orchestrator + DB stubbed — no LLM, no DB).

Token verification itself is covered in test_server_auth.py; here the identity
dependency is overridden so these tests exercise handler behaviour instead.
"""
import pytest
from fastapi.testclient import TestClient

from server.app import app
from server.auth import current_user_id

USER = "user-1"
TEAM = "team-1"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("server.app.require_membership", lambda *_: None)
    app.dependency_overrides[current_user_id] = lambda: USER
    yield TestClient(app)
    app.dependency_overrides.clear()


def _stub_persistence(monkeypatch, recorder=None):
    def _user_msg(user_id, team_id, thread_type, text):
        if recorder is not None:
            recorder["user"] = (user_id, team_id, thread_type, text)
        return "msg-user"

    def _ai_msg(team_id, thread_type, owner_id, text):
        if recorder is not None:
            recorder["ai"] = (team_id, thread_type, owner_id, text)
        return "msg-ai"

    monkeypatch.setattr("server.app._persist_user_message", _user_msg)
    monkeypatch.setattr("server.app._persist_ai_reply", _ai_msg)


def test_health_ok():
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_agent_turn_returns_reply(client, monkeypatch):
    def _stub(team_id, requester_id, user_text, trigger_type="user"):
        assert (team_id, requester_id, user_text) == (TEAM, USER, "status?")
        return {"run_id": "run-1", "reply": "All caught up.", "steps": []}

    monkeypatch.setattr("server.app.run_turn_sync", _stub)
    _stub_persistence(monkeypatch)
    resp = client.post("/agent/turn", json={"team_id": TEAM, "text": "status?"})
    assert resp.status_code == 200
    assert resp.json() == {
        "run_id": "run-1",
        "reply": "All caught up.",
        "user_message_id": "msg-user",
        "reply_message_id": "msg-ai",
    }


def test_turn_identity_comes_from_the_token_not_the_body(client, monkeypatch):
    """A body-supplied requester_id must not influence who the agent runs as."""
    seen = {}
    monkeypatch.setattr(
        "server.app.run_turn_sync",
        lambda team_id, requester_id, text, trigger_type="user": seen.update(
            requester=requester_id
        ) or {"run_id": "r", "reply": "ok", "steps": []},
    )
    _stub_persistence(monkeypatch)
    resp = client.post(
        "/agent/turn",
        json={"team_id": TEAM, "text": "hi", "requester_id": "someone-else"},
    )
    assert resp.status_code == 200
    assert seen["requester"] == USER


def test_private_turn_is_owned_by_the_requester(client, monkeypatch):
    """Private threads must name their owner, or nobody can read them back."""
    rec = {}
    monkeypatch.setattr(
        "server.app.run_turn_sync",
        lambda *a, **k: {"run_id": "r", "reply": "noted", "steps": []},
    )
    _stub_persistence(monkeypatch, rec)
    client.post("/agent/turn", json={"team_id": TEAM, "text": "hi"})
    assert rec["user"] == (USER, TEAM, "private", "hi")
    assert rec["ai"] == (TEAM, "private", USER, "noted")


def test_group_turn_has_no_thread_owner(client, monkeypatch):
    rec = {}
    monkeypatch.setattr(
        "server.app.run_turn_sync",
        lambda *a, **k: {"run_id": "r", "reply": "posted", "steps": []},
    )
    _stub_persistence(monkeypatch, rec)
    client.post(
        "/agent/turn",
        json={"team_id": TEAM, "text": "hi", "thread_type": "group"},
    )
    assert rec["ai"] == (TEAM, "group", None, "posted")


def test_empty_reply_is_not_persisted(client, monkeypatch):
    """A turn that produced no text must not leave a blank AI message behind."""
    rec = {}
    monkeypatch.setattr(
        "server.app.run_turn_sync",
        lambda *a, **k: {"run_id": "r", "reply": "", "steps": []},
    )
    _stub_persistence(monkeypatch, rec)
    resp = client.post("/agent/turn", json={"team_id": TEAM, "text": "hi"})
    assert resp.json()["reply_message_id"] is None
    assert "ai" not in rec


def test_unknown_thread_type_is_rejected(client):
    resp = client.post(
        "/agent/turn",
        json={"team_id": TEAM, "text": "hi", "thread_type": "broadcast"},
    )
    assert resp.status_code == 422


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


def test_consent_not_yours_is_404(client, monkeypatch):
    monkeypatch.setattr(
        "server.app.approve_consent",
        lambda *a: {"status": "not_approved", "reason": "not pending or not yours"},
    )
    resp = client.post("/consent/c-1/approve", json={"team_id": TEAM})
    assert resp.status_code == 404


def test_stale_consent_surfaces_as_conflict(client, monkeypatch):
    from shared.consent import ConsentError

    def _raise(*a):
        raise ConsentError("consent c-1 has expired")

    monkeypatch.setattr("server.app.approve_consent", _raise)
    resp = client.post("/consent/c-1/approve", json={"team_id": TEAM})
    assert resp.status_code == 409
