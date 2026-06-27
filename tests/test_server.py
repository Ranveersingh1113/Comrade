"""HTTP layer for the agent runtime (orchestrator stubbed — no LLM, no DB)."""
from fastapi.testclient import TestClient

from server.app import app

client = TestClient(app)


def test_health_ok():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_agent_turn_returns_reply(monkeypatch):
    def _stub(team_id, requester_id, user_text, trigger_type="user"):
        assert (team_id, requester_id, user_text) == ("team-1", "user-1", "status?")
        return {"run_id": "run-1", "reply": "All caught up.", "steps": []}

    # Patch where it is used (server.app imported the name).
    monkeypatch.setattr("server.app.run_turn_sync", _stub)
    resp = client.post(
        "/agent/turn",
        json={"team_id": "team-1", "requester_id": "user-1", "text": "status?"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"run_id": "run-1", "reply": "All caught up."}
