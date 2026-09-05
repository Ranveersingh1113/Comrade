"""Streaming turn: NDJSON frames, and the guards that run before the stream."""
import json

import pytest
from fastapi.testclient import TestClient

from server.app import app
from server.auth import current_user_id
from shared.config import settings
from tests._seed import A1, B1, TEAM_A, as_user


async def _fake_frames(team_id, run_id):
    for frame in (
        {"type": "run", "run_id": run_id},
        {"seq": 0, "type": "tool_call", "tool": "team_get_state", "args": {}},
        {"seq": 1, "type": "text", "text": "The demo is Friday."},
        {"type": "done", "run_id": run_id, "status": "done", "detail": None},
    ):
        yield json.dumps(frame) + "\n"


@pytest.fixture
def as_a1(monkeypatch):
    monkeypatch.setattr("server.app.enqueue_turn", lambda *_: "run-1")
    monkeypatch.setattr("server.app._run_frames", _fake_frames)
    app.dependency_overrides[current_user_id] = lambda: A1
    yield TestClient(app)
    app.dependency_overrides.clear()


def _frames(resp) -> list[dict]:
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def _general_thread() -> str:
    with as_user(A1) as conn:
        row = conn.execute(
            "select id from public.threads where team_id=%s and title='General'",
            (TEAM_A,),
        ).fetchone()
    return str(row[0])


def test_stream_emits_run_steps_then_final(seeded, as_a1):
    resp = as_a1.post(
        "/agent/turn/stream", json={"team_id": TEAM_A, "thread_id": _general_thread(), "text": "when is the demo?"}
    )
    assert resp.status_code == 200
    frames = _frames(resp)
    assert frames[0] == {"type": "run", "run_id": "run-1"}
    assert any(f["type"] == "tool_call" and f["tool"] == "team_get_state" for f in frames)
    assert any(f["type"] == "text" for f in frames)
    done = frames[-1]
    assert done["type"] == "done"
    assert done["status"] == "done"


def test_stream_enqueues_the_canonical_thread(
    seeded, as_a1, monkeypatch
):
    seen = {}

    def _record(team_id, requester_id, thread_id, text):
        seen.update(team_id=team_id, requester_id=requester_id, thread_id=thread_id, text=text)
        return "r"

    monkeypatch.setattr("server.app.enqueue_turn", _record)
    as_a1.post(
        "/agent/turn/stream",
        json={"team_id": TEAM_A, "text": "hi", "thread_id": _general_thread()},
    )
    assert seen == {"team_id": TEAM_A, "requester_id": A1, "thread_id": _general_thread(), "text": "hi"}


def test_final_frame_never_reaches_the_wire(seeded, as_a1):
    """`final` is the generator's internal signal; the client sees `done`."""
    resp = as_a1.post("/agent/turn/stream", json={"team_id": TEAM_A, "thread_id": _general_thread(), "text": "hi"})
    assert not any(f["type"] == "final" for f in _frames(resp))


def test_non_member_gets_403_not_a_stream(seeded, monkeypatch):
    """The guard must fail as a real status, never as a 200 whose body says no."""
    monkeypatch.setattr("server.app._run_frames", _fake_frames)
    app.dependency_overrides[current_user_id] = lambda: B1
    try:
        resp = TestClient(app).post(
            "/agent/turn/stream", json={"team_id": TEAM_A, "thread_id": _general_thread(), "text": "hi"}
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
    resp = as_a1.post("/agent/turn/stream", json={"team_id": TEAM_A, "thread_id": _general_thread(), "text": "hi"})
    assert resp.status_code == 429


