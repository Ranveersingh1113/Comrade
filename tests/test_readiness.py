"""Liveness and readiness are different questions, and conflating them lies.

🔴 /health once returned {"status": "ok"} without touching anything, so an
instance whose pool held dead handles reported itself healthy while every
request failed. That is fixed. This file pins the OTHER half: a process can be
perfectly alive and still unable to serve a turn — schema a release behind, or
no worker draining the queue — and a deploy that goes green on liveness alone
tells an operator nothing about either.
"""
import psycopg
import pytest
from fastapi.testclient import TestClient

from server.app import app
from shared.config import settings
from tests._seed import A1, TEAM_A


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def client():
    return TestClient(app)


def test_ready_reports_each_check_by_name(seeded, client):
    """One boolean would tell an operator that something is wrong and nothing
    about which thing — at 3am that is the whole difference."""
    body = client.get("/ready").json()
    assert body["status"] == "ready", body
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["migrations"] == "ok"
    assert body["checks"]["agent_queue"] == "ok"


def test_a_stalled_queue_is_not_ready(seeded, client, admin):
    """A run nobody has claimed for ten minutes means no worker is draining.
    The member's experience is that Comrade accepted their message and then did
    nothing, and readiness is where that has to surface."""
    thread_id = admin.execute(
        "select id from public.threads where team_id=%s and title='General'",
        (TEAM_A,),
    ).fetchone()[0]
    admin.execute(
        "insert into public.agent_runs"
        " (team_id, thread_id, requester_id, status, trigger_type, created_at)"
        " values (%s,%s,%s,'queued','user', now() - interval '30 minutes')",
        (TEAM_A, thread_id, A1),
    )

    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["checks"]["agent_queue"].startswith("1 run(s) queued")


def test_a_busy_worker_is_not_reported_as_a_missing_one(seeded, client, admin):
    """A run claimed and still executing is work in progress, not a stall.
    Readiness that flaps under load is readiness an operator learns to ignore."""
    thread_id = admin.execute(
        "select id from public.threads where team_id=%s and title='General'",
        (TEAM_A,),
    ).fetchone()[0]
    admin.execute(
        "insert into public.agent_runs"
        " (team_id, thread_id, requester_id, status, trigger_type, created_at)"
        " values (%s,%s,%s,'running','user', now() - interval '30 minutes')",
        (TEAM_A, thread_id, A1),
    )
    assert client.get("/ready").json()["checks"]["agent_queue"] == "ok"


def test_liveness_does_not_depend_on_the_queue(seeded, client, admin):
    """The split's whole point. A stalled queue must not restart the API — the
    API is not what is broken, and restarting it loses in-flight requests while
    fixing nothing."""
    thread_id = admin.execute(
        "select id from public.threads where team_id=%s and title='General'",
        (TEAM_A,),
    ).fetchone()[0]
    admin.execute(
        "insert into public.agent_runs"
        " (team_id, thread_id, requester_id, status, trigger_type, created_at)"
        " values (%s,%s,%s,'queued','user', now() - interval '30 minutes')",
        (TEAM_A, thread_id, A1),
    )
    assert client.get("/health").status_code == 200


def test_readiness_needs_no_authentication(seeded, client):
    """A load balancer has no session. This must not be behind the auth
    dependency, and it must not leak anything either — the checks name
    subsystems, never rows."""
    body = client.get("/ready").json()
    assert set(body["checks"]) <= {"database", "migrations", "agent_queue"}


# ---------------------------------------------------------------------------
# Draining
# ---------------------------------------------------------------------------

def test_both_workers_stop_after_the_item_in_hand():
    """A container stop is not a crash. An abandoned run recovers only when its
    lease expires — minutes of a member watching nothing happen — so SIGTERM
    finishes the current item and then exits."""
    import agent.worker as agent_worker
    import pipeline.worker as pipeline_worker

    for module in (agent_worker, pipeline_worker):
        assert hasattr(module, "_stopping"), f"{module.__name__} cannot be drained"
        assert hasattr(module, "_drain_on_signal")
        module._stopping.clear()


def test_a_drained_agent_worker_claims_nothing_more(monkeypatch):
    """The loop must check the flag, not merely own one."""
    import agent.worker as worker

    claims = []

    def _claim(worker_id):
        claims.append(worker_id)
        worker._stopping.set()       # stopped while this item was in hand
        return None

    monkeypatch.setattr(worker, "claim_next_run", _claim)
    worker._stopping.clear()
    try:
        worker.main()
    finally:
        worker._stopping.clear()

    assert len(claims) == 1, f"kept claiming after SIGTERM: {len(claims)} claims"
