"""Webhook -> github_activity: the codebase's first unauthenticated route.

findings §16.6 / phase-3 plan Task 3: verify the raw body, THEN parse, resolve
the team from `repository.full_name`, and enqueue an `ingest_github` job keyed
by the delivery id so a GitHub retry is a no-op. The handler then turns one
delivery into (at most) one `github_activity` row.

Security is the point of this route ("the first route with no JWT behind
it"), so these tests lead with the failure modes: no signature, wrong
signature, and the fail-closed empty-secret case — pinned here at the route,
not only in server/webhooks.py's own tests. Functional coverage (team
resolution, replay, unregistered repos, the handler's field mapping) follows.
"""
import hashlib
import hmac
import json

import psycopg
import pytest
from fastapi.testclient import TestClient

from pipeline.github import handle_github_job
from server.app import app
from shared.config import settings
from tests._seed import A1, TEAM_A, count

SECRET = "gh-test-secret"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _register_repo(full_name="acme/widgets", team_id=TEAM_A):
    conn = _admin()
    try:
        return conn.execute(
            "insert into public.github_repos (team_id, repo_full_name)"
            " values (%s,%s) returning id",
            (team_id, full_name),
        ).fetchone()[0]
    finally:
        conn.close()


def _push_body(full_name="acme/widgets"):
    return {
        "ref": "refs/heads/main",
        "repository": {"full_name": full_name},
        "head_commit": {
            "id": "abc123",
            "message": "fix: the thing\n\nlonger explanation here",
            "timestamp": "2026-08-30T12:00:00Z",
            "url": "https://github.com/acme/widgets/commit/abc123",
            "author": {"name": "Maya", "email": "maya@x.dev", "username": "maya-gh"},
        },
    }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", SECRET)
    return TestClient(app)


def _post(client, body, *, sign_with=SECRET, event="push", delivery="d-1"):
    raw = json.dumps(body).encode()
    headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery}
    if sign_with is not None:
        headers["X-Hub-Signature-256"] = _sign(raw, sign_with)
    return client.post("/webhooks/github", content=raw, headers=headers)


def _job_count(delivery: str) -> int:
    conn = _admin()
    try:
        return count(
            conn,
            "select count(*) from public.jobs"
            " where job_type='ingest_github' and dedupe_key=%s",
            (delivery,),
        )
    finally:
        conn.close()


# ---------- security: the point of this route ----------

def test_unsigned_request_is_rejected(seeded, client):
    _register_repo()
    resp = _post(client, _push_body(), sign_with=None, delivery="sec-1")
    assert resp.status_code == 401
    assert _job_count("sec-1") == 0


def test_wrongly_signed_request_is_rejected(seeded, client):
    _register_repo()
    resp = _post(client, _push_body(), sign_with="not-the-secret", delivery="sec-2")
    assert resp.status_code == 401
    assert _job_count("sec-2") == 0


def test_correctly_signed_request_for_a_registered_repo_is_accepted(seeded, client):
    _register_repo()
    resp = _post(client, _push_body(), delivery="sec-3")
    assert resp.status_code == 200
    assert _job_count("sec-3") == 1


def test_empty_configured_secret_refuses_even_a_correctly_signed_request(
    seeded, client, monkeypatch
):
    """Pinned here at the route, not only inside verify_signature's own tests:
    the fail-closed property must hold end to end. Signed correctly for
    SECRET, then the deployment's secret is unset out from under it."""
    _register_repo()
    raw = json.dumps(_push_body()).encode()
    signature = _sign(raw)
    monkeypatch.setattr(settings, "github_webhook_secret", "")
    resp = client.post(
        "/webhooks/github",
        content=raw,
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "sec-4",
            "X-Hub-Signature-256": signature,
        },
    )
    assert resp.status_code == 401
    assert _job_count("sec-4") == 0


# ---------- replay protection ----------

def test_the_same_delivery_id_twice_enqueues_exactly_one_job(seeded, client):
    _register_repo()
    body = _push_body()
    r1 = _post(client, body, delivery="replay-1")
    r2 = _post(client, body, delivery="replay-1")
    assert r1.status_code == 200 and r2.status_code == 200
    assert _job_count("replay-1") == 1


# ---------- unregistered repo: accepted, dropped, no oracle ----------

def test_an_unregistered_repo_is_accepted_and_dropped(seeded, client):
    resp = _post(client, _push_body(full_name="nobody/nothing"), delivery="unreg-1")
    assert resp.status_code == 200  # same 2xx as the registered case — no oracle
    assert _job_count("unreg-1") == 0


# ---------- the route does not pre-filter by event type ----------

def test_route_still_enqueues_a_job_for_an_event_type_the_handler_ignores(
    seeded, client
):
    """The handler no-ops on an unrecognised event (see below); the route
    enqueues regardless. Pre-filtering at the route would duplicate the
    handler's dispatch table in a second place."""
    _register_repo()
    resp = _post(client, _push_body(), event="star", delivery="unhandled-route-1")
    assert resp.status_code == 200
    assert _job_count("unhandled-route-1") == 1


# ---------- the handler: mapping + storage ----------

def _activity_row(team_id=TEAM_A):
    conn = _admin()
    try:
        return conn.execute(
            "select node_type, author_github, author_user_id, occurred_at, payload"
            " from public.github_activity where team_id=%s",
            (team_id,),
        ).fetchone()
    finally:
        conn.close()


def test_handler_writes_the_expected_row_for_a_push(seeded):
    _register_repo("acme/widgets")
    admin = _admin()
    try:
        admin.execute(
            "update public.profiles set github_username=%s where id=%s",
            ("maya-gh", A1),
        )
    finally:
        admin.close()

    handle_github_job(TEAM_A, {"event": "push", "body": _push_body()})

    node_type, author_github, author_user_id, occurred_at, payload = _activity_row()
    assert node_type == "commit"
    assert author_github == "maya-gh"
    assert str(author_user_id) == A1
    assert occurred_at is not None
    assert payload["sha"] == "abc123"
    assert payload["title"] == "fix: the thing"
    assert payload["body"] == "longer explanation here"


def test_unmapped_author_leaves_author_user_id_null(seeded):
    """No profile has github_username='maya-gh' in this test — an unmapped
    author is normal, not an error."""
    _register_repo("acme/widgets")
    handle_github_job(TEAM_A, {"event": "push", "body": _push_body()})
    _node_type, author_github, author_user_id, _occurred_at, _payload = _activity_row()
    assert author_github == "maya-gh"
    assert author_user_id is None


@pytest.mark.parametrize(
    "event,body,expected_node_type",
    [
        (
            "issues",
            {
                "repository": {"full_name": "acme/widgets"},
                "issue": {
                    "title": "t", "body": "b", "number": 3, "html_url": "u",
                    "state": "open", "user": {"login": "maya-gh"},
                    "updated_at": "2026-08-30T00:00:00Z",
                },
            },
            "issue",
        ),
        (
            "issue_comment",
            {
                "repository": {"full_name": "acme/widgets"},
                "issue": {"title": "t", "number": 3},
                "comment": {
                    "body": "b", "html_url": "u", "user": {"login": "maya-gh"},
                    "updated_at": "2026-08-30T00:00:00Z",
                },
            },
            "comment",
        ),
        (
            "pull_request_review",
            {
                "repository": {"full_name": "acme/widgets"},
                "pull_request": {"title": "t", "number": 3},
                "review": {
                    "body": "b", "html_url": "u", "state": "approved",
                    "user": {"login": "maya-gh"},
                    "submitted_at": "2026-08-30T00:00:00Z",
                },
            },
            "review",
        ),
    ],
)
def test_handler_maps_each_supported_event_to_its_node_type(
    seeded, event, body, expected_node_type
):
    _register_repo("acme/widgets")
    handle_github_job(TEAM_A, {"event": event, "body": body})
    node_type, *_ = _activity_row()
    assert node_type == expected_node_type


@pytest.mark.parametrize("merged,expected_node_type", [(True, "merge"), (False, "pr")])
def test_pull_request_merged_flag_selects_the_node_type(
    seeded, merged, expected_node_type
):
    _register_repo("acme/widgets")
    body = {
        "repository": {"full_name": "acme/widgets"},
        "pull_request": {
            "title": "t", "body": "b", "number": 9, "html_url": "u",
            "merged": merged, "state": "closed",
            "user": {"login": "maya-gh"},
            "merged_at": "2026-08-30T00:00:00Z" if merged else None,
            "updated_at": "2026-08-30T00:00:00Z",
        },
    }
    handle_github_job(TEAM_A, {"event": "pull_request", "body": body})
    node_type, _author, _uid, _occ, payload = _activity_row()
    assert node_type == expected_node_type
    assert payload["merged"] is merged


def test_an_unhandled_event_type_does_not_raise_and_writes_no_row(seeded):
    _register_repo("acme/widgets")
    handle_github_job(
        TEAM_A,
        {"event": "star", "body": {"repository": {"full_name": "acme/widgets"}}},
    )  # must not raise
    conn = _admin()
    try:
        n = count(
            conn,
            "select count(*) from public.github_activity where team_id=%s",
            (TEAM_A,),
        )
    finally:
        conn.close()
    assert n == 0
