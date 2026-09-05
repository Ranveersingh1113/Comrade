"""HTTP layer for the agent runtime (orchestrator + DB stubbed — no LLM, no DB).

Token verification itself is covered in test_server_auth.py; here the identity
dependency is overridden so these tests exercise handler behaviour instead.
"""
from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from server.app import ThreadScope, app
from server.auth import current_user_id

USER = "11111111-1111-1111-1111-111111111111"
TEAM = "22222222-2222-2222-2222-222222222222"
PRIVATE_THREAD = "33333333-3333-3333-3333-333333333333"
GROUP_THREAD = "44444444-4444-4444-4444-444444444444"


@pytest.fixture
def client(monkeypatch):
    # Both guards are stubbed: these tests exercise handler behaviour with no
    # DB. Membership is covered in test_server_auth, the budget in
    # test_server_budget.
    monkeypatch.setattr("server.app.require_membership", lambda *_: None)
    monkeypatch.setattr("server.app._check_turn_budget", lambda *_: None)
    def _thread(_user, _team, thread_id, legacy_type):
        resolved = str(thread_id) if thread_id else (
            GROUP_THREAD if legacy_type == "group" else PRIVATE_THREAD
        )
        is_group = resolved == GROUP_THREAD
        return ThreadScope(resolved, "group" if is_group else "private", None if is_group else USER)

    monkeypatch.setattr("server.app._resolve_thread", _thread)

    @contextmanager
    def _available(_thread_id):
        yield True

    monkeypatch.setattr("server.app.thread_lock", _available)
    app.dependency_overrides[current_user_id] = lambda: USER
    yield TestClient(app)
    app.dependency_overrides.clear()


def _stub_persistence(monkeypatch, recorder=None):
    def _user_msg(user_id, team_id, scope, text):
        if recorder is not None:
            recorder["user"] = (user_id, team_id, scope, text)
        return "msg-user"

    def _ai_msg(team_id, scope, text):
        if recorder is not None:
            recorder["ai"] = (team_id, scope, text)
        return "msg-ai"

    monkeypatch.setattr("server.app._persist_user_message", _user_msg)
    monkeypatch.setattr("server.app._persist_ai_reply", _ai_msg)


def test_health_ok():
    """Healthy now means reachable AND able to see the database — it used to
    mean only that a process was listening, which it reported cheerfully while
    every real request failed on a dead connection pool."""
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok"}


def test_agent_turn_returns_reply(client, monkeypatch):
    def _stub(team_id, requester_id, user_text, trigger_type="user", **kw):
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
        lambda team_id, requester_id, text, trigger_type="user", **kw: seen.update(
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


def test_runtime_is_told_the_thread_and_the_message_to_skip(client, monkeypatch):
    """History is thread-scoped, and the member's message is persisted BEFORE
    the turn runs — the runtime needs both facts or it replays the wrong
    conversation, or the current question twice."""
    seen = {}
    monkeypatch.setattr(
        "server.app.run_turn_sync",
        lambda *a, **kw: seen.update(kw) or
        {"run_id": "r", "reply": "ok", "steps": []},
    )
    _stub_persistence(monkeypatch)
    client.post(
        "/agent/turn",
        json={"team_id": TEAM, "text": "hi", "thread_id": GROUP_THREAD},
    )
    assert seen == {
        "thread_id": GROUP_THREAD,
        "exclude_message_id": "msg-user",
        "lock_held": True,
    }


def test_private_turn_carries_its_canonical_scope(client, monkeypatch):
    rec = {}
    monkeypatch.setattr(
        "server.app.run_turn_sync",
        lambda *a, **k: {"run_id": "r", "reply": "noted", "steps": []},
    )
    _stub_persistence(monkeypatch, rec)
    client.post("/agent/turn", json={"team_id": TEAM, "text": "hi"})
    assert rec["user"] == (
        USER, TEAM, ThreadScope(PRIVATE_THREAD, "private", USER), "hi",
    )
    assert rec["ai"] == (TEAM, ThreadScope(PRIVATE_THREAD, "private", USER), "noted")


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
    assert rec["ai"] == (TEAM, ThreadScope(GROUP_THREAD, "group", None), "posted")


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


def test_busy_turn_does_not_persist_an_orphaned_message(client, monkeypatch):
    """A rejected same-thread turn must not become history for a later run."""
    rec = {}
    monkeypatch.setattr(
        "server.app.run_turn_sync",
        lambda *a, **k: {"run_id": None, "reply": "", "steps": [], "busy": "busy"},
    )
    _stub_persistence(monkeypatch, rec)

    @contextmanager
    def _busy_lock(_thread_id):
        yield False

    monkeypatch.setattr("server.app.thread_lock", _busy_lock)

    resp = client.post("/agent/turn", json={"team_id": TEAM, "text": "hi"})

    assert resp.status_code == 409
    assert "user" not in rec


def test_inaccessible_thread_is_rejected_before_persisting(client, monkeypatch):
    rec = {}
    monkeypatch.setattr(
        "server.app._resolve_thread",
        lambda *_: (_ for _ in ()).throw(HTTPException(status_code=404)),
    )
    _stub_persistence(monkeypatch, rec)

    resp = client.post(
        "/agent/turn", json={"team_id": TEAM, "thread_id": GROUP_THREAD, "text": "hi"}
    )

    assert resp.status_code == 404
    assert rec == {}


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
