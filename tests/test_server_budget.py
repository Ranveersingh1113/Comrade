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


def _seed_runs(n: int, *, minutes_ago: int = 0) -> None:
    """Spend `n` turns against the team's hourly bucket.

    This used to insert agent_runs rows, because the cap was a `count(*)` over
    that table. It is now a reservation on one usage_buckets row — the change
    that makes the cap hold under concurrency (shared/usage.py) — so seeding
    past spend means writing the bucket the reservation would have written.

    `minutes_ago` still means what it did: the bucket is keyed by hour, so
    spend from two hours ago lands on a different row and falls out of the
    window for free rather than by a date comparison.

    🔴 It DEFAULTED to 5, and the bucket is `date_trunc('hour', ...)`. So for
    the first five minutes of every hour, `now() - 5 minutes` truncated to the
    PREVIOUS hour: the spend was seeded onto a row `reserve_turn` never looks
    at, and the cap test failed for four minutes in every sixty. A full suite
    run takes nine minutes, so it crossed that window regularly — and a test
    that fails on the clock is one everybody learns to re-run rather than
    read.
    """
    conn = _admin()
    try:
        conn.execute(
            "insert into public.usage_buckets (team_id, bucket, turns, tokens)"
            " values (%s, date_trunc('hour', now() - make_interval(mins => %s)), %s, %s)"
            " on conflict (team_id, bucket) do update"
            "   set turns = usage_buckets.turns + excluded.turns,"
            "       tokens = usage_buckets.tokens + excluded.tokens",
            (TEAM_A, minutes_ago, n, n * settings.agent_tokens_estimate),
        )
    finally:
        conn.close()


@pytest.fixture
def as_a1():
    app.dependency_overrides[current_user_id] = lambda: A1
    yield TestClient(app)
    app.dependency_overrides.clear()


#: Any well-formed uuid. _resolve_thread is stubbed, so it is never looked up
#: — but the body must still VALIDATE, or FastAPI answers 422 and a test
#: asserting 429 fails for a reason that has nothing to do with the budget.
THREAD = "33333333-3333-3333-3333-333333333333"


def _turn(client):
    return client.post(
        "/agent/turn",
        json={"team_id": TEAM_A, "text": "status?", "thread_id": THREAD},
    )


def _stub_turn(monkeypatch):
    """Budget checks must never actually run the agent.

    The turn endpoint enqueues now instead of running inline, so the names
    stubbed here follow it: resolve the thread, write the run. run_turn_sync
    and _persist_ai_reply went with that change, and monkeypatch.setattr
    raises on a name that no longer exists — which is the behaviour worth
    having, since a stub for a function nobody calls is a test guarding air.
    """
    monkeypatch.setattr("server.app._resolve_thread", lambda *_: THREAD)
    monkeypatch.setattr(
        "server.app.enqueue_turn",
        lambda *a, **k: "44444444-4444-4444-4444-444444444444",
    )


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
            "/agent/turn",
            json={"team_id": TEAM_A, "text": "hi", "thread_id": THREAD},
        )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 403
