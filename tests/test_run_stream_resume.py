"""Reattaching to a durable run after the connection drops.

🔴 THE DEFECT. The browser held a run only in the memory of the POST that
started it. A refresh, a sleeping laptop or a dropped proxy connection ended
the stream, and nothing reconnected: the typing indicator stopped, no reply
appeared, and the member had no way to tell a finished turn from a severed
one. The run itself was fine the whole time — durable, leased, still working.

Two things were missing. A cursor, so a reattach replays only what the browser
has not already seen instead of every event from the beginning. And a
distinction between "this run finished" and "this run is waiting for a human",
which arrived as the same terminal frame.
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from server.app import app, _run_frames
from server.auth import current_user_id
from tests._seed import A1, TEAM_A, as_user


def _drain(team_id: str, run_id: str, **kwargs) -> list[dict]:
    # The viewer is required now: `_run_frames` revalidates access before it
    # emits anything, so a stream cannot outlive the permission that opened it
    # (fix.md F04). These tests are about cursors, polling and framing, so the
    # check is stubbed to a yes and the authorization itself is covered by
    # tests/test_stream_revocation.py.
    import server.app as app_module

    original = app_module._may_watch
    app_module._may_watch = lambda *_a, **_k: True

    async def go() -> list[str]:
        return [
            line async for line in
            _run_frames(team_id, run_id, viewer_id=A1, **kwargs)
        ]

    try:
        return [json.loads(line) for line in asyncio.run(go()) if line.strip()]
    finally:
        app_module._may_watch = original


def _run(status: str, steps: list[dict]) -> dict:
    return {
        "id": "run-1", "thread_id": "t", "requester_id": A1, "status": status,
        "attempts": 1, "worker_id": "w", "lease_expires_at": None,
        "finished_at": None, "last_error": None, "steps": steps,
    }


STEPS = [
    {"seq": 0, "type": "tool_call", "tool": "team_get_state"},
    {"seq": 1, "type": "tool_result", "tool": "team_get_state"},
    {"seq": 2, "type": "text", "text": "Friday."},
]


def test_reattaching_with_a_cursor_replays_only_later_events(monkeypatch):
    monkeypatch.setattr("server.app.get_run", lambda *_: _run("done", STEPS))

    frames = _drain(TEAM_A, "run-1", after_seq=1)

    replayed = [f["seq"] for f in frames if "seq" in f]
    assert replayed == [2], "everything at or before the cursor is already on screen"


def test_attaching_without_a_cursor_replays_the_whole_run(monkeypatch):
    """A refresh has nothing on screen; it needs the run from the beginning."""
    monkeypatch.setattr("server.app.get_run", lambda *_: _run("done", STEPS))

    frames = _drain(TEAM_A, "run-1")

    assert [f["seq"] for f in frames if "seq" in f] == [0, 1, 2]


def test_a_waiting_run_is_not_reported_as_finished(monkeypatch):
    """A run waiting on a consent card has not produced its answer yet.

    Reported as `done`, the browser stopped its indicator and the member read
    the silence as a failed turn — while a card sat above the composer asking
    them for the very permission that would continue it."""
    monkeypatch.setattr(
        "server.app.get_run", lambda *_: _run("waiting_for_permission", STEPS),
    )

    frames = _drain(TEAM_A, "run-1")

    assert frames[-1]["type"] == "status"
    assert frames[-1]["status"] == "waiting_for_permission"
    assert not any(f["type"] == "done" for f in frames)


def test_a_finished_run_still_ends_with_done(monkeypatch):
    monkeypatch.setattr("server.app.get_run", lambda *_: _run("failed", STEPS))

    frames = _drain(TEAM_A, "run-1")

    assert frames[-1]["type"] == "done"
    assert frames[-1]["status"] == "failed"


def test_the_opening_frame_carries_the_run_status(monkeypatch):
    """So a reattach knows whether it is resuming live work or reading history."""
    monkeypatch.setattr("server.app.get_run", lambda *_: _run("done", STEPS))

    first = _drain(TEAM_A, "run-1")[0]

    assert first["type"] == "run" and first["run_id"] == "run-1"
    assert first["status"] == "done"


@pytest.fixture
def as_a1(monkeypatch):
    monkeypatch.setattr("server.app.require_membership", lambda *_: None)
    monkeypatch.setattr("server.app._visible_run", lambda *_: None)
    monkeypatch.setattr("server.app.get_run", lambda *_: _run("done", STEPS))
    app.dependency_overrides[current_user_id] = lambda: A1
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_the_run_stream_endpoint_honours_the_cursor(as_a1):
    resp = as_a1.get(f"/agent/runs/run-1/stream?team_id={TEAM_A}&after_seq=1")

    assert resp.status_code == 200
    frames = [json.loads(l) for l in resp.text.splitlines() if l.strip()]
    assert [f["seq"] for f in frames if "seq" in f] == [2]
