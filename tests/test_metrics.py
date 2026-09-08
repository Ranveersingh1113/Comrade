"""The numbers an operator needs to know whether a limit is set right.

🔴 THE DEFECT. Nothing counted anything. `/ready` answers "is it broken",
which is the wrong question for every decision an operator actually makes:
whether the hourly token cap is too tight, whether the compiler is falling
behind, whether previews fail often enough to matter, how many turns were
refused this hour and why. Each of those was a SQL query somebody had to write
from memory against a schema they did not have in front of them, at the moment
they could least afford it.
"""
import psycopg
import pytest
from fastapi.testclient import TestClient

from server.app import app
from shared.config import settings
from tests._seed import A1, TEAM_A, TEAM_B


#: The endpoint is off unless a token is configured, so the tests about what
#: it REPORTS configure one; the tests about who may ask override it.
METRICS_TOKEN = "test-metrics-token"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "comrade_metrics_token", METRICS_TOKEN)


class _Client(TestClient):
    def get(self, url, **kwargs):
        kwargs.setdefault("headers", {"Authorization": f"Bearer {METRICS_TOKEN}"})
        return super().get(url, **kwargs)


@pytest.fixture
def client():
    return _Client(app)


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _metrics(client) -> dict:
    response = client.get("/metrics")
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------

def test_queue_age_is_reported_rather_than_a_verdict(seeded, client):
    """`/ready` says "stalled" past a threshold. An operator choosing that
    threshold needs the number it is compared against."""
    conn = _admin()
    try:
        conn.execute("delete from public.jobs")
        conn.execute(
            "insert into public.jobs (team_id, job_type, payload, created_at,"
            " available_at) values (%s,'ingest_github','{}',"
            " now() - interval '7 minutes', now() - interval '7 minutes')",
            (TEAM_A,),
        )
        metrics = _metrics(client)
    finally:
        conn.execute("delete from public.jobs")
        conn.close()

    assert metrics["pipeline"]["pending"] == 1
    assert 6 <= metrics["pipeline"]["oldest_pending_seconds"] / 60 <= 8


def test_lease_loss_is_counted(seeded, client):
    """Work reclaimed from a worker that stopped answering. A rising count is
    how you find a worker being OOM-killed without anything else saying so."""
    conn = _admin()
    try:
        conn.execute("delete from public.jobs")
        conn.execute(
            "insert into public.jobs (team_id, job_type, payload, status,"
            " picked_at, lease_expires_at, worker_id)"
            " values (%s,'ingest_github','{}','processing',"
            " now() - interval '2 hours', now() - interval '90 minutes','gone')",
            (TEAM_A,),
        )
        metrics = _metrics(client)
    finally:
        conn.execute("delete from public.jobs")
        conn.close()

    assert metrics["pipeline"]["leases_expired"] == 1


def test_spend_this_hour_is_reported(seeded, client):
    """The two caps are per team per hour, and nobody could see what was being
    spent against them without writing the query."""
    conn = _admin()
    try:
        conn.execute("delete from public.usage_buckets")
        conn.execute(
            "insert into public.usage_buckets (team_id, bucket, turns, tokens)"
            " values (%s, date_trunc('hour', now()), 4, 40000)", (TEAM_A,),
        )
        metrics = _metrics(client)
    finally:
        conn.execute("delete from public.usage_buckets")
        conn.close()

    assert metrics["usage"]["turns_this_hour"] == 4
    assert metrics["usage"]["tokens_this_hour"] == 40000


def test_the_caps_are_reported_next_to_the_spend(seeded, client):
    """A number with nothing to compare it to is not a metric."""
    metrics = _metrics(client)

    assert metrics["usage"]["turns_cap"] == settings.agent_turns_per_hour
    assert metrics["usage"]["tokens_cap"] == settings.agent_tokens_per_hour


def test_runs_are_counted_by_how_they_ended(seeded, client):
    """Failures, cancellations and turns parked waiting for a permission are
    three different problems, and the ratio between them is the signal."""
    conn = _admin()
    try:
        thread_id = conn.execute(
            "select id from public.threads where team_id=%s and title='General'",
            (TEAM_A,),
        ).fetchone()[0]
        for status in ("done", "failed", "waiting_for_permission"):
            conn.execute(
                "insert into public.agent_runs (team_id, thread_id, requester_id,"
                " status, trigger_type) values (%s,%s,%s,%s,'user')",
                (TEAM_A, thread_id, A1, status),
            )
        metrics = _metrics(client)
    finally:
        conn.execute("delete from public.agent_runs where team_id=%s", (TEAM_A,))
        conn.close()

    by_status = metrics["agent"]["runs_by_status"]
    assert by_status["failed"] >= 1
    assert by_status["waiting_for_permission"] >= 1


def test_compiler_lag_is_reported(seeded, client):
    """How far behind the wiki is from the conversation. Nothing fails when
    this grows; the product just quietly stops knowing things."""
    metrics = _metrics(client)

    assert "compiler_lag_seconds" in metrics["pipeline"]


def test_a_team_that_never_compiles_is_not_hidden_by_one_that_does(seeded, client):
    """🔴 The lag was measured against `max(chat_through)` across ALL teams,
    so one team compiling five minutes ago made every older message everywhere
    look already-compiled. The team whose pipeline has been broken for a week
    — the only one this metric exists to find — was the one it could not see.
    """
    conn = _admin()
    try:
        thread_id = conn.execute(
            "select id from public.threads where team_id=%s and title='General'",
            (TEAM_A,),
        ).fetchone()[0]
        conn.execute("delete from public.memory_compilations")
        # Team A has said something and nobody has compiled it in two days.
        conn.execute(
            "insert into public.messages (team_id, thread_id, sender_kind,"
            " sender_id, body, created_at) values"
            " (%s,%s,'user',%s,'this is uncompiled', now() - interval '2 days')",
            (TEAM_A, thread_id, A1),
        )
        # Team B compiled a minute ago, which must not speak for team A.
        conn.execute(
            "insert into public.memory_compilations (team_id, status, trigger,"
            " chat_through) values"
            " (%s,'done','scheduled', now() - interval '1 minute')",
            (TEAM_B,),
        )
        lag = _metrics(client)["pipeline"]["compiler_lag_seconds"]
    finally:
        conn.execute("delete from public.memory_compilations")
        conn.close()

    assert lag > 24 * 3600, (
        f"a two-day backlog was reported as {lag}s of lag"
    )


# ---------------------------------------------------------------------------
# What it must not do
# ---------------------------------------------------------------------------

def test_no_team_is_named(seeded, client):
    """Aggregates only. An operations endpoint that lists which team is
    spending what is a cross-team disclosure wearing a monitoring hat."""
    conn = _admin()
    try:
        conn.execute("delete from public.usage_buckets")
        for team in (TEAM_A, TEAM_B):
            conn.execute(
                "insert into public.usage_buckets (team_id, bucket, turns, tokens)"
                " values (%s, date_trunc('hour', now()), 1, 1000)", (team,),
            )
        body = client.get("/metrics").text
    finally:
        conn.execute("delete from public.usage_buckets")
        conn.close()

    assert TEAM_A not in body and TEAM_B not in body


def test_no_content_is_reported(seeded, client):
    """Counts and ages. Never a subject, a title, an error string or a name."""
    body = client.get("/metrics").text

    for leak in ("last_error", "subject", "title", "body", "payload"):
        assert leak not in body, f"/metrics reports `{leak}`"


def test_it_survives_an_empty_deployment(seeded, client):
    """A brand new install has no runs, no jobs and no buckets, and that is
    the moment an operator is most likely to look at this."""
    conn = _admin()
    try:
        conn.execute("delete from public.jobs")
        conn.execute("delete from public.usage_buckets")
        metrics = _metrics(client)
    finally:
        conn.close()

    assert metrics["pipeline"]["pending"] == 0
    assert metrics["pipeline"]["oldest_pending_seconds"] == 0


# ---------------------------------------------------------------------------
# Who may ask
# ---------------------------------------------------------------------------

def test_metrics_is_off_unless_a_token_is_configured(seeded, client, monkeypatch):
    """🔴 It answered anybody. Queue depth, hourly spend and failure counts
    are not a member's data, but they are not the internet's either — and
    "public unless someone remembers to put a proxy in front" is the fail-open
    default this codebase keeps having to remove.

    404 rather than 401 when unconfigured: an endpoint that is not turned on
    should not advertise that it exists.
    """
    monkeypatch.setattr(settings, "comrade_metrics_token", "")

    assert client.get("/metrics").status_code == 404


def test_a_wrong_token_is_refused(seeded, client, monkeypatch):
    monkeypatch.setattr(settings, "comrade_metrics_token", "the-real-one")

    assert client.get("/metrics", headers={
        "Authorization": "Bearer nearly-the-real-one"}).status_code == 401


def test_the_right_token_is_let_through(seeded, client, monkeypatch):
    monkeypatch.setattr(settings, "comrade_metrics_token", "the-real-one")

    response = client.get(
        "/metrics", headers={"Authorization": "Bearer the-real-one"})

    assert response.status_code == 200
    assert "pipeline" in response.json()


def test_the_token_is_compared_in_constant_time(seeded, client):
    """A metrics token is guessable by timing exactly like any other secret,
    and `==` on strings returns early on the first differing byte."""
    import inspect

    import server.app as api

    source = inspect.getsource(api.metrics)
    assert "compare_digest" in source, (
        "the token is compared with ==, which leaks its prefix to a timer"
    )


# ---------------------------------------------------------------------------
# Lag has to mean the same thing capture means
# ---------------------------------------------------------------------------

def _clear_compilations():
    conn = _admin()
    try:
        conn.execute("delete from public.memory_compilations")
    finally:
        conn.close()


def _say(thread_id, *, sender_kind="user", deleted=None, days=2, sender=A1):
    conn = _admin()
    try:
        conn.execute(
            "insert into public.messages (team_id, thread_id, sender_kind,"
            " sender_id, body, deleted_scope, created_at) values"
            " (%s,%s,%s,%s,'something', %s, now() - make_interval(days => %s))",
            (TEAM_A, thread_id, sender_kind,
             sender if sender_kind == "user" else None, deleted, days),
        )
    finally:
        conn.close()


def _thread(visibility="team", title="General"):
    conn = _admin()
    try:
        if visibility == "team":
            return conn.execute(
                "select id from public.threads where team_id=%s and title=%s",
                (TEAM_A, title),
            ).fetchone()[0]
        thread_id = conn.execute(
            "insert into public.threads (team_id, title, visibility, kind,"
            " created_by) values (%s,%s,%s,'discussion',%s) returning id",
            (TEAM_A, title, visibility, A1),
        ).fetchone()[0]
        conn.execute(
            "insert into public.thread_participants (thread_id, team_id,"
            " user_id, added_by) values (%s,%s,%s,%s)",
            (thread_id, TEAM_A, A1, A1),
        )
        return thread_id
    finally:
        conn.close()


@pytest.mark.parametrize("kind", ["private", "ai", "deleted"])
def test_activity_capture_ignores_does_not_move_the_lag(seeded, client, kind):
    """🔴 The metric compared EVERY message to the compilation cursor, while
    capture only ever looks at undeleted user messages in team-visible
    threads. A team whose conversation is entirely private, or entirely the
    agent talking, reported a steadily growing compiler backlog that no amount
    of working pipeline could ever clear.

    Measured as a DELTA rather than an absolute zero: the seed leaves real
    capturable messages behind, so "the number is zero" would be a claim about
    the fixture. What matters is that adding something capture ignores does
    not move it — and the message added here is far older than anything the
    seed has, so it would dominate the maximum if it counted at all.
    """
    _clear_compilations()
    before = _metrics(client)["pipeline"]["compiler_lag_seconds"]

    if kind == "private":
        _say(_thread("restricted", "Security review"), days=90)
    elif kind == "ai":
        _say(_thread(), sender_kind="ai", days=90)
    else:
        _say(_thread(), deleted="everyone", days=90)

    after = _metrics(client)["pipeline"]["compiler_lag_seconds"]
    # A BOUNDED delta, not equality and not an absolute. The reported lag is
    # an age, so both readings creep forward by the seconds between the two
    # requests; and the seed leaves genuinely capturable messages of its own,
    # so the absolute number is a fact about the fixture. What must not happen
    # is the 90-day-old ignored message becoming the maximum.
    assert after - before < 60, (
        f"a message capture ignores became the reported backlog: {before}s ->"
        f" {after}s"
    )


def test_a_real_uncaptured_message_still_reports_its_age(seeded, client):
    """The other half. A guard that ignored everything would pass every case
    above and report a healthy pipeline through any outage."""
    _clear_compilations()
    before = _metrics(client)["pipeline"]["compiler_lag_seconds"]

    _say(_thread(), days=90)

    after = _metrics(client)["pipeline"]["compiler_lag_seconds"]
    assert after > before, "a genuinely uncaptured message did not register"
    assert after > 89 * 24 * 3600, after


def test_only_a_finished_compilation_moves_the_cursor(seeded, client):
    """A compilation that is still running, or failed, has not captured
    anything — treating its `chat_through` as progress hides a real backlog."""
    _clear_compilations()
    _say(_thread(), days=3)
    conn = _admin()
    try:
        conn.execute(
            "insert into public.memory_compilations (team_id, status, trigger,"
            " chat_through) values (%s,'running','scheduled', now())", (TEAM_A,),
        )
    finally:
        conn.close()

    assert _metrics(client)["pipeline"]["compiler_lag_seconds"] > 2 * 24 * 3600, (
        "a compilation that has not finished was treated as progress"
    )
