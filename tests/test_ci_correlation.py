"""Telling the thread that its pull request failed.

🔴 THE DEFECT. A pull request Comrade opened returned its number to the
consent flow and was then forgotten — no row, no thread mapping, nothing. So
when GitHub reported that the checks had failed, there was no way to say WHOSE
work had failed: the delivery was ingested as repository activity for the wiki,
and the thread that produced the change never heard about it. A member had to
go and look.
"""
import psycopg
import pytest

from pipeline.ci import record_check_result, record_pull_request
from shared.config import settings
from tests._seed import A1, A2, B1, TEAM_A, as_user


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _thread(conn, visibility="team", title="Fix the importer"):
    thread_id = str(conn.execute(
        "insert into public.threads (team_id, title, visibility, kind, created_by)"
        " values (%s,%s,%s,'discussion',%s) returning id",
        (TEAM_A, title, visibility, A1),
    ).fetchone()[0])
    if visibility == "restricted":
        conn.execute(
            "insert into public.thread_participants (thread_id, team_id, user_id,"
            " added_by) values (%s,%s,%s,%s)",
            (thread_id, TEAM_A, A1, A1),
        )
    return thread_id


def _clear():
    conn = _admin()
    try:
        conn.execute("delete from public.github_check_results where team_id=%s", (TEAM_A,))
        conn.execute("delete from public.github_pull_requests where team_id=%s", (TEAM_A,))
    finally:
        conn.close()


def _check_run(*, sha, conclusion="failure", status="completed", pr=None,
               branch="comrade/abc123"):
    return {
        "check_run": {
            "name": "pytest", "status": status, "conclusion": conclusion,
            "html_url": "https://github.com/acme/app/runs/1", "head_sha": sha,
            "check_suite": {"head_branch": branch},
            "pull_requests": [{"number": pr}] if pr else [],
        }
    }


def _results():
    conn = _admin()
    try:
        return conn.execute(
            "select check_name, conclusion, stale, thread_id::text"
            " from public.github_check_results where team_id=%s"
            " order by reported_at",
            (TEAM_A,),
        ).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------

def test_a_failing_check_lands_on_the_thread_that_wrote_the_change(seeded):
    """🔴 Nothing connected them. The delivery became repository trivia and
    the people who wrote the change heard nothing."""
    _clear()
    conn = _admin()
    try:
        thread_id = _thread(conn)
    finally:
        conn.close()
    record_pull_request(
        TEAM_A, "acme/app", 12, "comrade/abc123",
        thread_id=thread_id, action_hash="abc123", head_sha="sha-1",
    )

    record_check_result(
        TEAM_A, "acme/app", "check_run", _check_run(sha="sha-1", pr=12),
        delivery_id="d-1",
    )

    assert _results() == [("pytest", "failure", False, thread_id)]


def test_a_check_is_matched_by_branch_when_it_names_no_pull_request(seeded):
    """GitHub does not always populate `pull_requests` — a check on a fork, or
    one that arrives before the PR is linked. The branch is the other handle,
    and it is the one Comrade chose."""
    _clear()
    conn = _admin()
    try:
        thread_id = _thread(conn)
    finally:
        conn.close()
    record_pull_request(
        TEAM_A, "acme/app", 12, "comrade/abc123",
        thread_id=thread_id, action_hash="abc123", head_sha="sha-1",
    )

    record_check_result(
        TEAM_A, "acme/app", "check_run", _check_run(sha="sha-1"),
        delivery_id="d-2",
    )

    assert _results()[0][3] == thread_id


def test_a_check_for_work_this_team_did_not_open_is_still_recorded(seeded):
    """Somebody else's pull request on a connected repository. Recorded with
    no thread rather than dropped: it is real repository history."""
    _clear()

    record_check_result(
        TEAM_A, "acme/app", "check_run",
        _check_run(sha="sha-9", branch="someone-elses-branch"),
        delivery_id="d-3",
    )

    rows = _results()
    assert len(rows) == 1 and rows[0][3] is None


# ---------------------------------------------------------------------------
# Deliveries that should change nothing
# ---------------------------------------------------------------------------

def test_a_redelivery_is_not_recorded_twice(seeded):
    """🔴 The job queue dedupes an ACTIVE delivery — only while the job is
    pending or processing. A manual redelivery from GitHub's UI after the
    first one finished would have been ingested again."""
    _clear()

    record_check_result(TEAM_A, "acme/app", "check_run",
                        _check_run(sha="sha-1"), delivery_id="same")
    record_check_result(TEAM_A, "acme/app", "check_run",
                        _check_run(sha="sha-1"), delivery_id="same")

    assert len(_results()) == 1


def test_a_check_still_running_is_not_news(seeded):
    """Telling somebody their work is "queued" is noise, and the same delivery
    arrives again with an answer."""
    _clear()

    record_check_result(
        TEAM_A, "acme/app", "check_run",
        _check_run(sha="sha-1", status="in_progress", conclusion=None),
        delivery_id="d-4",
    )

    assert _results() == []


def test_a_result_for_a_commit_the_branch_moved_past_is_marked_stale(seeded):
    """🔴 A failure about an old commit may already be fixed. Kept rather than
    dropped — an audit that omits the failures nobody acted on is not an
    audit — but marked, so nothing decides anything from it."""
    _clear()
    conn = _admin()
    try:
        thread_id = _thread(conn)
    finally:
        conn.close()
    record_pull_request(
        TEAM_A, "acme/app", 12, "comrade/abc123",
        thread_id=thread_id, action_hash="abc123", head_sha="sha-2",
    )

    record_check_result(
        TEAM_A, "acme/app", "check_run", _check_run(sha="sha-1", pr=12),
        delivery_id="d-5",
    )

    assert _results()[0][2] is True, "an old-head result must be marked stale"


def test_a_result_for_the_current_head_is_not_stale(seeded):
    _clear()
    conn = _admin()
    try:
        thread_id = _thread(conn)
    finally:
        conn.close()
    record_pull_request(
        TEAM_A, "acme/app", 12, "comrade/abc123",
        thread_id=thread_id, action_hash="abc123", head_sha="sha-2",
    )

    record_check_result(
        TEAM_A, "acme/app", "check_run", _check_run(sha="sha-2", pr=12),
        delivery_id="d-6",
    )

    assert _results()[0][2] is False


def test_opening_the_same_pull_request_twice_converges(seeded):
    """Opening a pull request is deliberately safe to repeat, so this has to
    converge the same way rather than raising."""
    _clear()
    conn = _admin()
    try:
        thread_id = _thread(conn)
    finally:
        conn.close()

    for _ in range(2):
        record_pull_request(
            TEAM_A, "acme/app", 12, "comrade/abc123",
            thread_id=thread_id, action_hash="abc123", head_sha="sha-1",
        )

    conn = _admin()
    try:
        n = conn.execute(
            "select count(*) from public.github_pull_requests where team_id=%s",
            (TEAM_A,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1


# ---------------------------------------------------------------------------
# Who may read it
# ---------------------------------------------------------------------------

def test_a_check_on_restricted_work_is_invisible_to_non_participants(seeded):
    _clear()
    conn = _admin()
    try:
        thread_id = _thread(conn, visibility="restricted", title="Security fix")
    finally:
        conn.close()
    record_pull_request(
        TEAM_A, "acme/app", 12, "comrade/abc123",
        thread_id=thread_id, action_hash="abc123", head_sha="sha-1",
    )
    record_check_result(
        TEAM_A, "acme/app", "check_run", _check_run(sha="sha-1", pr=12),
        delivery_id="d-7",
    )

    with as_user(A2) as conn:
        rows = conn.execute(
            "select 1 from public.github_check_results where team_id=%s", (TEAM_A,)
        ).fetchall()

    assert rows == []


def test_a_participant_sees_it(seeded):
    _clear()
    conn = _admin()
    try:
        thread_id = _thread(conn, visibility="restricted", title="Security fix")
    finally:
        conn.close()
    record_pull_request(
        TEAM_A, "acme/app", 12, "comrade/abc123",
        thread_id=thread_id, action_hash="abc123", head_sha="sha-1",
    )
    record_check_result(
        TEAM_A, "acme/app", "check_run", _check_run(sha="sha-1", pr=12),
        delivery_id="d-8",
    )

    with as_user(A1) as conn:
        rows = conn.execute(
            "select 1 from public.github_check_results where team_id=%s", (TEAM_A,)
        ).fetchall()

    assert len(rows) == 1


def test_another_team_sees_none_of_it(seeded):
    _clear()
    record_check_result(
        TEAM_A, "acme/app", "check_run", _check_run(sha="sha-1"),
        delivery_id="d-9",
    )

    with as_user(B1) as conn:
        rows = conn.execute(
            "select 1 from public.github_check_results where team_id=%s", (TEAM_A,)
        ).fetchall()

    assert rows == []


@pytest.mark.parametrize("event", ["check_suite", "workflow_run"])
def test_the_other_terminal_check_events_are_understood(seeded, event):
    """A team's CI reports through whichever of these its setup uses."""
    _clear()
    body = {
        event: {
            "name": "build", "status": "completed", "conclusion": "success",
            "head_sha": "sha-1", "head_branch": "comrade/abc123",
            "html_url": "https://github.com/acme/app/actions/1",
            "url": "https://api.github.com/x", "pull_requests": [],
        }
    }

    record_check_result(TEAM_A, "acme/app", event, body, delivery_id=f"d-{event}")

    assert len(_results()) == 1
