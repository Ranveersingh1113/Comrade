"""Per-team hourly turn budget: the lid on the LLM spend hole."""
import psycopg
import pytest
from fastapi.testclient import TestClient

from server.app import app
from server.auth import current_user_id
from shared.config import settings
from tests._seed import A1, B1, TEAM_A


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _seed_runs(n: int, *, minutes_ago: int = 5) -> None:
    conn = _admin()
    try:
        for _ in range(n):
            conn.execute(
                "insert into public.agent_runs (team_id, trigger_type, status,"
                " created_at) values (%s,'user','done', now() - make_interval(mins => %s))",
                (TEAM_A, minutes_ago),
            )
    finally:
        conn.close()


@pytest.fixture
def as_a1():
    app.dependency_overrides[current_user_id] = lambda: A1
    yield TestClient(app)
    app.dependency_overrides.clear()


def _turn(client):
    return client.post("/agent/turn", json={"team_id": TEAM_A, "text": "status?"})


def _stub_turn(monkeypatch):
    """Budget checks must never actually run the agent."""
    monkeypatch.setattr(
        "server.app.run_turn_sync",
        lambda *a, **k: {"run_id": "r", "reply": "ok", "steps": []},
    )
    monkeypatch.setattr("server.app._persist_user_message", lambda *a: "m1")
    monkeypatch.setattr("server.app._persist_ai_reply", lambda *a: "m2")


def test_turn_is_refused_once_the_team_hits_its_cap(seeded, as_a1, monkeypatch):
    monkeypatch.setattr(settings, "agent_turns_per_hour", 2)
    _seed_runs(2)
    resp = _turn(as_a1)
    assert resp.status_code == 429
    assert "agent turns" in resp.json()["detail"]


def test_turn_is_allowed_below_the_cap(seeded, as_a1, monkeypatch):
    monkeypatch.setattr(settings, "agent_turns_per_hour", 5)
    _stub_turn(monkeypatch)
    _seed_runs(1)
    assert _turn(as_a1).status_code == 200


def test_old_runs_fall_out_of_the_window(seeded, as_a1, monkeypatch):
    monkeypatch.setattr(settings, "agent_turns_per_hour", 1)
    _stub_turn(monkeypatch)
    _seed_runs(3, minutes_ago=120)      # yesterday's spend doesn't count
    assert _turn(as_a1).status_code == 200


def test_zero_disables_the_cap(seeded, as_a1, monkeypatch):
    monkeypatch.setattr(settings, "agent_turns_per_hour", 0)
    _stub_turn(monkeypatch)
    _seed_runs(50)
    assert _turn(as_a1).status_code == 200


def test_non_member_gets_403_not_429(seeded, monkeypatch):
    """Never leak that a team exists, or how busy it is, to an outsider."""
    monkeypatch.setattr(settings, "agent_turns_per_hour", 1)
    _seed_runs(5)
    app.dependency_overrides[current_user_id] = lambda: B1
    try:
        resp = TestClient(app).post(
            "/agent/turn", json={"team_id": TEAM_A, "text": "hi"}
        )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 403
