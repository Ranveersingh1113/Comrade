"""Streaming turn: NDJSON frames, and the guards that run before the stream."""
import json

import pytest
from fastapi.testclient import TestClient

from server.app import app
from server.auth import current_user_id
from shared.config import settings
from tests._seed import A1, B1, TEAM_A


async def _fake_stream(team_id, requester_id, user_text, trigger_type="user"):
    yield {"type": "run", "run_id": "run-1"}
    yield {"seq": 0, "type": "tool_call", "tool": "team_get_state", "args": {}}
    yield {"seq": 1, "type": "text", "text": "The demo is Friday."}
    yield {"type": "final", "run_id": "run-1", "reply": "The demo is Friday."}


@pytest.fixture
def as_a1(monkeypatch):
    monkeypatch.setattr("server.app.stream_turn", _fake_stream)
    monkeypatch.setattr("server.app._persist_user_message", lambda *a: "msg-user")
    monkeypatch.setattr("server.app._persist_ai_reply", lambda *a: "msg-ai")
    app.dependency_overrides[current_user_id] = lambda: A1
    yield TestClient(app)
    app.dependency_overrides.clear()


def _frames(resp) -> list[dict]:
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def test_stream_emits_run_steps_then_final(seeded, as_a1):
    resp = as_a1.post(
        "/agent/turn/stream", json={"team_id": TEAM_A, "text": "when is the demo?"}
    )
    assert resp.status_code == 200
    frames = _frames(resp)
    assert frames[0] == {"type": "run", "run_id": "run-1"}
    assert any(f["type"] == "tool_call" and f["tool"] == "team_get_state" for f in frames)
    assert any(f["type"] == "text" for f in frames)
    done = frames[-1]
    assert done["type"] == "done"
    assert done["user_message_id"] == "msg-user"
    assert done["reply_message_id"] == "msg-ai"


def test_final_frame_never_reaches_the_wire(seeded, as_a1):
    """`final` is the generator's internal signal; the client sees `done`."""
    resp = as_a1.post("/agent/turn/stream", json={"team_id": TEAM_A, "text": "hi"})
    assert not any(f["type"] == "final" for f in _frames(resp))


def test_non_member_gets_403_not_a_stream(seeded, monkeypatch):
    """The guard must fail as a real status, never as a 200 whose body says no."""
    monkeypatch.setattr("server.app.stream_turn", _fake_stream)
    app.dependency_overrides[current_user_id] = lambda: B1
    try:
        resp = TestClient(app).post(
            "/agent/turn/stream", json={"team_id": TEAM_A, "text": "hi"}
        )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_over_budget_gets_429_not_a_stream(seeded, as_a1, monkeypatch):
    monkeypatch.setattr(settings, "agent_turns_per_hour", 1)
    import psycopg

    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        conn.execute(
            "insert into public.agent_runs (team_id, trigger_type, status)"
            " values (%s,'user','done')",
            (TEAM_A,),
        )
    finally:
        conn.close()
    resp = as_a1.post("/agent/turn/stream", json={"team_id": TEAM_A, "text": "hi"})
    assert resp.status_code == 429


def test_empty_reply_persists_no_ai_message(seeded, as_a1, monkeypatch):
    async def _silent(team_id, requester_id, user_text, trigger_type="user"):
        yield {"type": "run", "run_id": "run-2"}
        yield {"type": "final", "run_id": "run-2", "reply": ""}

    monkeypatch.setattr("server.app.stream_turn", _silent)
    resp = as_a1.post("/agent/turn/stream", json={"team_id": TEAM_A, "text": "hi"})
    assert _frames(resp)[-1]["reply_message_id"] is None


def test_a_failed_turn_streams_an_error_frame(seeded, as_a1, monkeypatch):
    async def _boom(team_id, requester_id, user_text, trigger_type="user"):
        yield {"type": "run", "run_id": "run-3"}
        raise RuntimeError("gemini exploded")

    monkeypatch.setattr("server.app.stream_turn", _boom)
    resp = as_a1.post("/agent/turn/stream", json={"team_id": TEAM_A, "text": "hi"})
    last = _frames(resp)[-1]
    assert last["type"] == "error"
    assert "gemini exploded" in last["detail"]
