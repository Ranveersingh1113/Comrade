"""The CI feature, exercised where it was actually broken.

🔴 THE DEFECTS. T25 built check-result correlation and it never ran once.

F24 — the webhook route passed the delivery id as a fifth POSITIONAL argument
to a keyword-only parameter. Every supported check event raised
`TypeError: takes 4 positional arguments but 5 were given`, was swallowed by a
broad `except`, and the delivery was acknowledged with no record written.

F25 — `_create_pr` returns `pr_url` and `created`. `shared/consent.py` reads
`result["number"]`, so every pull request Comrade opened raised KeyError into
another broad `except` and no thread was ever correlated with its work.

T25's tests called `record_check_result(...)` and `record_pull_request(...)`
directly, with the right keywords, so they were green over both broken call
sites. These tests go through the ROUTE and through the real `_create_pr`
adapter with its transport mocked, because that is where the defects were.
"""
import hashlib
import hmac
import json

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from server.app import app
from shared.config import settings
from tests._seed import A1, TEAM_A

SECRET = "test-webhook-secret"


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def hooked(seeded, monkeypatch):
    """A team with a connected repository and a signed webhook."""
    monkeypatch.setattr(settings, "github_webhook_secret", SECRET)
    conn = _admin()
    try:
        conn.execute(
            "delete from public.github_check_results where team_id=%s", (TEAM_A,))
        conn.execute(
            "delete from public.github_pull_requests where team_id=%s", (TEAM_A,))
        conn.execute("delete from public.jobs where team_id=%s", (TEAM_A,))
        conn.execute(
            "insert into public.github_installations (team_id, installation_id,"
            " account_login) values (%s, 987654, 'acme')"
            " on conflict do nothing", (TEAM_A,),
        )
        # `github_repos.installation_id` is the GitHub id, not the row id.
        conn.execute(
            "insert into public.github_repos (team_id, repo_full_name,"
            " installation_id) values (%s,'acme/app',987654)"
            " on conflict do nothing", (TEAM_A,),
        )
    finally:
        conn.close()
    return TestClient(app)


def _results() -> list[tuple]:
    conn = _admin()
    try:
        return conn.execute(
            "select check_name, conclusion, delivery_id"
            "  from public.github_check_results where team_id=%s", (TEAM_A,),
        ).fetchall()
    finally:
        conn.close()


def _deliver(client, body: dict, *, delivery: str, event: str = "check_run"):
    raw = json.dumps(body).encode()
    return client.post(
        "/webhooks/github", content=raw,
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": _sign(raw),
            "Content-Type": "application/json",
        },
    )


CHECK = {
    "repository": {"full_name": "acme/app"},
    "check_run": {
        "name": "pytest", "status": "completed", "conclusion": "failure",
        "html_url": "https://github.com/acme/app/runs/1", "head_sha": "sha-1",
        "check_suite": {"head_branch": "comrade/abc123"},
        "pull_requests": [{"number": 12}],
    },
}


# ---------------------------------------------------------------------------
# F24: through the route
# ---------------------------------------------------------------------------

def test_a_signed_check_delivery_records_a_result(hooked):
    """🔴 It recorded nothing. The route's call raised TypeError on every
    single check event and the delivery was acknowledged anyway."""
    from pipeline import worker

    assert _deliver(hooked, CHECK, delivery="d-1").status_code < 300

    # The delivery is durable as a JOB; the handler correlates it.
    while worker.run_once(worker_id="ci-wiring-test"):
        if _results():
            break

    assert [(name, conclusion) for name, conclusion, _ in _results()] == [
        ("pytest", "failure")
    ]


def test_a_redelivery_does_not_duplicate_the_result(hooked):
    from pipeline import worker

    for _ in range(2):
        _deliver(hooked, CHECK, delivery="d-same")
        while worker.run_once(worker_id="ci-wiring-test"):
            pass

    assert len(_results()) == 1


def test_a_correlation_failure_leaves_the_job_to_retry(hooked, monkeypatch):
    """🔴 The route logged and continued, so a failure lost the result for
    good. In the handler it is a failed job, which the queue retries."""
    from pipeline import worker

    _deliver(hooked, CHECK, delivery="d-retry")

    calls = {"n": 0}

    def _flaky(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("the database blinked")

    monkeypatch.setattr("pipeline.ci.record_check_result", _flaky)
    worker.run_once(worker_id="ci-wiring-test")
    monkeypatch.undo()

    conn = _admin()
    try:
        status = conn.execute(
            "select status from public.jobs where team_id=%s"
            "  and dedupe_key='d-retry'", (TEAM_A,),
        ).fetchone()
    finally:
        conn.close()
    assert calls["n"] == 1
    assert status and status[0] == "pending", (
        "a failed correlation did not leave the job to be retried"
    )


# ---------------------------------------------------------------------------
# F25: through the real adapter
# ---------------------------------------------------------------------------

def _transport(handler):
    return httpx.MockTransport(handler)


def test_the_adapter_returns_the_pull_request_number(monkeypatch):
    """🔴 It returned `pr_url` and `created` only, so the consent flow's
    `result["number"]` raised KeyError and nothing was correlated."""
    from pipeline import repo_pr

    def _handler(request):
        return httpx.Response(201, json={
            "html_url": "https://github.com/acme/app/pull/12",
            "number": 12, "head": {"sha": "sha-1"},
        })

    monkeypatch.setattr(
        repo_pr.httpx, "post",
        lambda *a, **k: _handler(None),
    )

    result = repo_pr._create_pr(
        "acme/app", "comrade/abc123", "master", "t", "b", "token",
    )

    assert result["number"] == 12
    assert result["head_sha"] == "sha-1"
    assert result["created"] is True


def test_the_already_open_path_returns_it_too(monkeypatch):
    """The RETRY path, which is exactly when the correlation record is the
    thing that is missing and needs writing."""
    from pipeline import repo_pr

    monkeypatch.setattr(
        repo_pr.httpx, "post",
        lambda *a, **k: httpx.Response(
            422, json={"message": "A pull request already exists"},
            request=httpx.Request("POST", "https://api.github.com"),
        ),
    )
    monkeypatch.setattr(
        repo_pr.httpx, "get",
        lambda *a, **k: httpx.Response(200, json=[{
            "html_url": "https://github.com/acme/app/pull/12",
            "number": 12, "head": {"sha": "sha-9"},
        }], request=httpx.Request("GET", "https://api.github.com")),
    )

    result = repo_pr._create_pr(
        "acme/app", "comrade/abc123", "master", "t", "b", "token",
    )

    assert result["number"] == 12
    assert result["head_sha"] == "sha-9"
    assert result["created"] is False
