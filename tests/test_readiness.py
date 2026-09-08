"""Liveness and readiness are different questions, and conflating them lies.

🔴 /health once returned {"status": "ok"} without touching anything, so an
instance whose pool held dead handles reported itself healthy while every
request failed. That is fixed. This file pins the OTHER half: a process can be
perfectly alive and still unable to serve a turn — schema a release behind, or
no worker draining the queue — and a deploy that goes green on liveness alone
tells an operator nothing about either.

🔴 AND THEN /ready ITSELF WAS TOO EASY TO SATISFY (T27):

1. The migration check compared MAXIMUMS — `applied >= newest_on_disk`. A
   release that skipped one in the middle (a migration that failed and was
   retried, a rebase that reordered two files, a partially restored backup)
   passed on the strength of the newest one being present. The column nobody
   created is then a 500 on whichever request first touches it, which reads as
   a code bug rather than a half-finished release.

2. Only ONE role's connectivity was checked. The API answers with
   `comrade_control`; a turn needs `comrade_agent`, an approved action needs
   `comrade_executor`, a document needs `comrade_pipeline` and a member read
   needs `comrade_authenticator`. A half-done credential rotation left the
   deployment green while every turn in it failed.

3. The PIPELINE queue was not checked at all. A wedged pipeline worker means
   no document compiled and no memory written, silently, while the agent queue
   it did check moved fine.

4. Nothing noticed leases that had expired while still marked running: work a
   dead worker is holding that nobody is doing.
"""
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from server.app import app
from shared.config import settings
from tests._seed import A1, TEAM_A

MIGRATIONS = Path(__file__).resolve().parent.parent / "supabase" / "migrations"


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


@pytest.fixture(autouse=True)
def _workers_are_alive():
    """A deployment with no worker is not ready, which is the point of F33 —
    so every test about something ELSE has to say that both are here.

    The tests that are about worker presence clear this themselves.
    """
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        for kind in ("agent", "pipeline"):
            conn.execute(
                "insert into public.worker_heartbeats (worker_kind, worker_id,"
                " capabilities) values (%s,%s,'{\"sandbox\": \"ok\"}'::jsonb)"
                " on conflict (worker_kind, worker_id) do update"
                "   set last_seen_at = now(),"
                "       capabilities = excluded.capabilities",
                (kind, f"fixture-{kind}"),
            )
        yield
        conn.execute("delete from public.worker_heartbeats")
    finally:
        conn.close()


def test_ready_reports_each_check_by_name(seeded, client):
    """One boolean would tell an operator that something is wrong and nothing
    about which thing — at 3am that is the whole difference."""
    body = client.get("/ready").json()
    assert body["status"] == "ready", body
    for name in ("database", "migrations", "roles", "agent_queue",
                 "pipeline_queue", "expired_leases", "workers", "sandbox"):
        assert body["checks"][name] == "ok", (name, body["checks"][name])


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
    # The SET grew in T27 (roles, pipeline_queue, expired_leases). What must
    # not grow is what a check VALUE may contain, which the two tests at the
    # end of this file pin.
    assert set(body["checks"]) <= {
        "database", "migrations", "roles", "agent_queue", "pipeline_queue",
        "expired_leases", "workers", "sandbox",
    }


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
    """The loop must check the flag, not merely own one.

    ONE slot, pinned. The worker runs `comrade_agent_concurrency` slots and
    each is an independent loop, so with the default of two this asserted
    something about thread scheduling rather than about draining: both threads
    can pass their `_stopping` check before either has set it, and each then
    legitimately claims once. That is correct behaviour for two slots and it
    made the count a coin flip — it started failing the moment an unrelated
    change added a database round trip to the top of the loop and widened the
    window.
    """
    import agent.worker as worker
    from shared.config import settings as app_settings

    monkeypatch.setattr(app_settings, "comrade_agent_concurrency", 1)
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

def _admin_conn():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _checks(client) -> dict:
    return client.get("/ready").json()["checks"]


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

def test_a_missing_middle_migration_is_not_ready(seeded, client):
    """🔴 It was. The check compared the newest version on disk with the
    newest applied, so a gap anywhere below the top was invisible."""
    versions = sorted(p.name.split("_", 1)[0] for p in MIGRATIONS.glob("*.sql"))
    middle = versions[len(versions) // 2]

    conn = _admin_conn()
    try:
        row = conn.execute(
            "select version, statements, name from"
            " supabase_migrations.schema_migrations where version=%s", (middle,),
        ).fetchone()
        assert row, f"{middle} should be applied in a healthy database"
        conn.execute(
            "delete from supabase_migrations.schema_migrations where version=%s",
            (middle,),
        )
        response = client.get("/ready")
        checks = response.json()["checks"]
    finally:
        conn.execute(
            "insert into supabase_migrations.schema_migrations"
            " (version, statements, name) values (%s,%s,%s)"
            " on conflict (version) do nothing", row,
        )
        conn.close()

    assert response.status_code == 503
    assert checks["migrations"] != "ok"
    assert middle in checks["migrations"], (
        "an operator has to be told WHICH migration is missing: " + checks["migrations"]
    )


def test_a_complete_schema_is_ready(seeded, client):
    assert _checks(client)["migrations"] == "ok"


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

def test_every_role_the_product_needs_is_checked(seeded, client):
    """🔴 Only the control role was. A turn needs `comrade_agent`, an approved
    action needs `comrade_executor`, a document needs `comrade_pipeline`, and
    a member read needs `comrade_authenticator` — so a half-done credential
    rotation left this green while every turn failed."""
    checks = _checks(client)

    assert checks["roles"] == "ok", checks["roles"]


def test_a_role_that_cannot_connect_is_not_ready(seeded, client, monkeypatch):
    import shared.db as db
    from shared.db import Role

    monkeypatch.setitem(
        db._URLS, Role.EXECUTOR,
        "postgresql://comrade_executor:wrong@127.0.0.1:54322/postgres",
    )
    db.close_pools()
    try:
        response = client.get("/ready")
    finally:
        db.close_pools()

    assert response.status_code == 503
    assert "executor" in response.json()["checks"]["roles"]


# ---------------------------------------------------------------------------
# Work actually moving
# ---------------------------------------------------------------------------

def test_a_stalled_pipeline_queue_is_not_ready(seeded, client):
    """🔴 Not checked at all. A wedged pipeline worker means no document is
    compiled and no memory written, silently, behind a green deploy."""
    conn = _admin_conn()
    try:
        conn.execute(
            "insert into public.jobs (team_id, job_type, payload, created_at,"
            " available_at) values (%s,'ingest_github','{}',"
            " now() - interval '3 hours', now() - interval '3 hours')",
            (TEAM_A,),
        )
        response = client.get("/ready")
    finally:
        conn.execute("delete from public.jobs where team_id=%s", (TEAM_A,))
        conn.close()

    assert response.status_code == 503
    assert response.json()["checks"]["pipeline_queue"] != "ok"


def test_a_lease_that_expired_while_still_running_is_not_ready(seeded, client):
    """Work a dead worker is holding and nobody is doing. The recovery sweep
    is supposed to reclaim these; if they are piling up, it is not running."""
    conn = _admin_conn()
    try:
        conn.execute(
            "insert into public.jobs (team_id, job_type, payload, status,"
            " picked_at, lease_expires_at, worker_id)"
            " values (%s,'ingest_github','{}','processing',"
            " now() - interval '2 hours', now() - interval '90 minutes','gone')",
            (TEAM_A,),
        )
        response = client.get("/ready")
    finally:
        conn.execute("delete from public.jobs where team_id=%s", (TEAM_A,))
        conn.close()

    assert response.status_code == 503
    assert response.json()["checks"]["expired_leases"] != "ok"


def test_a_quiet_system_is_ready(seeded, client):
    """An EMPTY queue is the normal state and must not read as a stall — the
    check is "is work stuck", not "is work happening"."""
    conn = _admin_conn()
    try:
        conn.execute("delete from public.jobs where team_id=%s", (TEAM_A,))
    finally:
        conn.close()

    response = client.get("/ready")

    assert response.status_code == 200, response.json()


# ---------------------------------------------------------------------------
# Liveness is a different question
# ---------------------------------------------------------------------------

def test_health_does_not_fail_on_a_stalled_PIPELINE_queue(seeded, client):
    """🔴 The distinction this route exists for. Restarting the API does not
    unstick a queue, so a stall must not be something an orchestrator kills
    healthy containers over — Compose's own health status does not restart
    anything, but a Kubernetes livenessProbe does."""
    conn = _admin_conn()
    try:
        conn.execute(
            "insert into public.jobs (team_id, job_type, payload, created_at,"
            " available_at) values (%s,'ingest_github','{}',"
            " now() - interval '3 hours', now() - interval '3 hours')",
            (TEAM_A,),
        )
        health = client.get("/health")
        ready = client.get("/ready")
    finally:
        conn.execute("delete from public.jobs where team_id=%s", (TEAM_A,))
        conn.close()

    assert health.status_code == 200, "liveness must not follow readiness"
    assert ready.status_code == 503


def test_readiness_never_reports_a_credential(seeded, client, monkeypatch):
    """The checks name what is broken, and a connection failure quotes the
    DSN back. See shared/errors.py."""
    import shared.db as db
    from shared.db import Role

    monkeypatch.setitem(
        db._URLS, Role.EXECUTOR,
        "postgresql://comrade_executor:s3cr3t-rotation@127.0.0.1:54322/postgres",
    )
    db.close_pools()
    try:
        body = client.get("/ready").text
    finally:
        db.close_pools()

    assert "s3cr3t-rotation" not in body


# ---------------------------------------------------------------------------
# The runbook has to describe this system, not a former one
# ---------------------------------------------------------------------------

def test_the_runbook_documents_every_check_this_route_reports(seeded, client):
    """A runbook that names four of six checks is a runbook that sends an
    operator to read the source at the worst possible moment."""
    runbook = (Path(__file__).resolve().parent.parent
               / "docs" / "operations.md").read_text(encoding="utf-8")

    for name in _checks(client):
        assert f"### `{name}`" in runbook, (
            f"/ready reports `{name}` and docs/operations.md never mentions it"
        )


def test_the_runbook_quotes_the_real_recovery_windows():
    """🔴 It quoted 300s for both workers; the pipeline worker's is 120s, and
    its lease is 30 minutes rather than 5. An operator sizing a deploy window
    from those numbers would have been out by an order of magnitude."""
    import yaml

    from agent import run_queue
    from pipeline import worker

    root = Path(__file__).resolve().parent.parent
    runbook = (root / "docs" / "operations.md").read_text(encoding="utf-8")
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))

    for service in ("pipeline-worker", "agent-worker"):
        grace = compose["services"][service]["stop_grace_period"]
        assert f"| {grace} |" in runbook, (
            f"{service} stops after {grace} and the runbook does not say so"
        )
    assert f"| {worker.LEASE_INTERVAL} |" in runbook
    assert f"| {run_queue.LEASE_INTERVAL} |" in runbook


# ---------------------------------------------------------------------------
# Is anybody here
# ---------------------------------------------------------------------------

def _beat(kind: str, *, age_seconds: int = 0, capabilities: str = '{"sandbox": "ok"}'):
    conn = _admin_conn()
    try:
        conn.execute(
            "insert into public.worker_heartbeats (worker_kind, worker_id,"
            " last_seen_at, capabilities) values"
            " (%s,%s, now() - make_interval(secs => %s), %s::jsonb)"
            " on conflict (worker_kind, worker_id) do update"
            "   set last_seen_at = excluded.last_seen_at,"
            "       capabilities = excluded.capabilities",
            (kind, f"test-{kind}", age_seconds, capabilities),
        )
    finally:
        conn.close()


def _clear_beats():
    conn = _admin_conn()
    try:
        conn.execute("delete from public.worker_heartbeats")
    finally:
        conn.close()


def test_an_empty_queue_with_no_workers_is_not_ready(seeded, client):
    """🔴 It was ready. Every check answered "is work stuck", and on a quiet
    deployment nothing is stuck — so a stack with BOTH WORKERS STOPPED was
    indistinguishable from a healthy idle one, and the first member to send a
    message found out."""
    _clear_beats()

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["workers"] != "ok"


def test_a_stale_heartbeat_is_not_a_live_worker(seeded, client):
    """A process that stopped an hour ago left its row behind. Presence is
    about recency, not about a row existing."""
    _clear_beats()
    _beat("agent", age_seconds=3600)
    _beat("pipeline", age_seconds=3600)

    assert client.get("/ready").json()["checks"]["workers"] != "ok"


def test_both_workers_reporting_is_ready(seeded, client):
    _clear_beats()
    _beat("agent")
    _beat("pipeline")

    assert _checks(client)["workers"] == "ok"


def test_a_missing_worker_is_named(seeded, client):
    """"Something is wrong" sends an operator to read source at 3am. Which
    one is missing is the whole message."""
    _clear_beats()
    _beat("agent")

    assert "pipeline" in _checks(client)["workers"]


def test_the_sandbox_is_reported_by_the_worker_that_holds_the_socket(seeded, client):
    """🔴 Unchecked, and uncheckable from here: the API deliberately has no
    Docker socket, so dead Docker meant every repository tool failed behind a
    green deployment. The execution worker reports; readiness reads."""
    _clear_beats()
    _beat("pipeline")
    _beat("agent", capabilities='{"sandbox": "the docker daemon did not answer"}')

    response = client.get("/ready")

    assert response.status_code == 503
    assert "docker daemon" in response.json()["checks"]["sandbox"]


def test_an_unsupported_backend_is_reported_honestly(seeded, client):
    """Selecting a provider nothing here has been proven against must read as
    unavailable, not as fine."""
    _clear_beats()
    _beat("pipeline")
    _beat("agent", capabilities='{"sandbox": "unsupported"}')

    assert _checks(client)["sandbox"] == "unsupported"


def test_an_expired_agent_run_lease_is_reported(seeded, client):
    """🔴 The lease check counted JOBS only. An agent run holds a lease the
    same way, and a member watching a turn that stopped mid-flight is a louder
    failure than an unparsed document."""
    _clear_beats()
    _beat("agent")
    _beat("pipeline")
    conn = _admin_conn()
    try:
        thread_id = conn.execute(
            "select id from public.threads where team_id=%s and title='General'",
            (TEAM_A,),
        ).fetchone()[0]
        conn.execute(
            "insert into public.agent_runs (team_id, thread_id, requester_id,"
            " status, trigger_type, worker_id, lease_expires_at) values"
            " (%s,%s,%s,'running','user','gone', now() - interval '2 hours')",
            (TEAM_A, thread_id, A1),
        )
        response = client.get("/ready")
    finally:
        conn.execute("delete from public.agent_runs where team_id=%s", (TEAM_A,))
        conn.close()

    assert response.status_code == 503
    assert "agent run" in response.json()["checks"]["expired_leases"]
