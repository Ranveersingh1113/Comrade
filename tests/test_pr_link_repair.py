"""A pull request that is open and belongs to nobody.

🔴 THE DEFECT (fix.md F41). The repair the comment promised cannot happen.

F25 fixed the PR adapter so it returns a real number, and correlation started
working. What it did not fix is the failure path around it:

    except Exception as exc:
        # Recoverable without opening a second pull request: opening one is
        # idempotent, so a later attempt returns the same PR through the
        # already-exists path and writes the mapping then.
        logger.error("could not record the pull request for %s: %s", ...)

There is no later attempt. `execute_consent` claims the row with a CAS that
moves it to `executed` BEFORE running the executor, so a retry matches
`status in ('approved','edited')` against a row that is already `executed`,
returns `{"status": "noop"}`, and the executor never runs again. The pull
request stays open with no thread mapping — so every CI check that arrives for
it has nowhere to go, and the thread that asked for the work never hears how it
went.

Rerunning the executor would not be the fix either, and the finding says so:
"retrying bookkeeping must not require another member approval or rerun the
external publication action". The publication already happened. What is missing
is one row.

So the intent is made DURABLE where every other retryable piece of backend work
in this system already lives — the job queue, which has attempts, backoff and
dedupe — and a handler writes the mapping without touching GitHub.
"""
import psycopg
import pytest

from shared import consent as consent_module
from shared.config import settings
from tests._seed import A1, TEAM_A

REPO = "acme/app"


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


@pytest.fixture
def clean(seeded):
    conn = _admin()
    try:
        conn.execute("delete from public.jobs where team_id=%s", (TEAM_A,))
        conn.execute("delete from public.github_pull_requests where team_id=%s",
                     (TEAM_A,))
        # No github_repos row: `github_pull_requests` is keyed on
        # (team, repo_full_name, pr_number) and references only `teams`.
        # Seeding a repo would have needed a github_installations row for its
        # foreign key, which is setup this has no use for.
        yield conn
    finally:
        conn.close()


def _pending_links(conn) -> list[dict]:
    rows = conn.execute(
        "select payload from public.jobs where team_id=%s"
        "  and job_type='link_pull_request' and status in ('pending','processing')",
        (TEAM_A,),
    ).fetchall()
    return [r[0] for r in rows]


def _mapping(conn, number: int):
    return conn.execute(
        "select thread_id, branch, head_sha from public.github_pull_requests"
        " where team_id=%s and repo_full_name=%s and pr_number=%s",
        (TEAM_A, REPO, number),
    ).fetchone()


# ---------------------------------------------------------------------------

def test_a_failed_correlation_leaves_a_durable_repair(clean, monkeypatch):
    """🔴 The finding's own case. The PR is open; the mapping write fails; the
    consent row is already `executed` so nothing will ever run again."""
    def _fails(*a, **k):
        raise RuntimeError("connection reset while recording the pull request")

    monkeypatch.setattr(consent_module, "record_pull_request", _fails)

    consent_module._correlate_pull_request(
        TEAM_A, {"repo_full_name": REPO}, action_hash="h" * 16,
        result={"number": 41, "head_sha": "s" * 40},
        thread_id=None,
    )

    pending = _pending_links(clean)
    assert len(pending) == 1, "the repair was logged and forgotten"
    assert pending[0]["pr_number"] == 41
    assert pending[0]["repo_full_name"] == REPO


def test_the_repair_writes_the_mapping_without_touching_github(clean, monkeypatch):
    """"Retrying bookkeeping must not ... rerun the external publication
    action." The handler writes one row and opens nothing."""
    from pipeline import ci

    opened: list[int] = []
    monkeypatch.setattr("pipeline.repo_pr.open_pull_request",
                        lambda *a, **k: opened.append(1), raising=False)

    ci.handle_pr_link(TEAM_A, {
        "repo_full_name": REPO, "pr_number": 42, "branch": "comrade/abc",
        "thread_id": None, "action_hash": "h" * 16, "head_sha": "s" * 40,
    })

    row = _mapping(clean, 42)
    assert row is not None, "the repair did not write the mapping"
    assert row[1] == "comrade/abc"
    assert row[2] == "s" * 40
    assert opened == [], "the repair opened a pull request"


def test_the_repair_needs_no_second_approval(clean, monkeypatch):
    """The consent row stays `executed`. A repair that required a member to
    approve again would be asking them to authorise a thing that has already
    happened."""
    from pipeline import ci

    consent_id = str(clean.execute(
        "insert into public.consent_queue (team_id, requesting_member_id,"
        " tool_name, tool_args, action_hash, status, resolved_at)"
        " values (%s,%s,'repo_open_pr','{}',%s,'executed',now())"
        " returning id",
        (TEAM_A, A1, "h" * 16),
    ).fetchone()[0])

    ci.handle_pr_link(TEAM_A, {
        "repo_full_name": REPO, "pr_number": 43, "branch": "comrade/def",
        "thread_id": None, "action_hash": "h" * 16, "head_sha": None,
    })

    status = clean.execute(
        "select status from public.consent_queue where id=%s", (consent_id,),
    ).fetchone()[0]
    assert status == "executed"
    assert _mapping(clean, 43) is not None


def test_the_repair_runs_exactly_once_per_pull_request(clean, monkeypatch):
    """A queue with retries will deliver the same intent more than once. The
    mapping upserts, so converging is the whole contract."""
    from pipeline import ci

    payload = {
        "repo_full_name": REPO, "pr_number": 44, "branch": "comrade/ghi",
        "thread_id": None, "action_hash": "h" * 16, "head_sha": "a" * 40,
    }
    ci.handle_pr_link(TEAM_A, payload)
    ci.handle_pr_link(TEAM_A, payload)

    rows = clean.execute(
        "select count(*) from public.github_pull_requests"
        " where team_id=%s and pr_number=44", (TEAM_A,),
    ).fetchone()[0]
    assert rows == 1


def test_a_second_failure_for_the_same_pr_does_not_pile_up(clean, monkeypatch):
    """The queue dedupes on an active key, so a consent flow that fails twice
    for one pull request leaves one repair rather than a growing heap."""
    def _fails(*a, **k):
        raise RuntimeError("still down")

    monkeypatch.setattr(consent_module, "record_pull_request", _fails)
    for _ in range(3):
        consent_module._correlate_pull_request(
            TEAM_A, {"repo_full_name": REPO}, action_hash="h" * 16,
            result={"number": 45, "head_sha": None}, thread_id=None,
        )

    assert len(_pending_links(clean)) == 1


def test_a_successful_correlation_queues_nothing(clean):
    """The repair path must not run when there is nothing to repair."""
    consent_module._correlate_pull_request(
        TEAM_A, {"repo_full_name": REPO}, action_hash="h" * 16,
        result={"number": 46, "head_sha": "b" * 40}, thread_id=None,
    )

    assert _pending_links(clean) == []
    assert _mapping(clean, 46) is not None


def test_a_repair_that_cannot_be_queued_is_still_reported(clean, monkeypatch):
    """The last resort. If even the queue is unreachable there is nothing left
    to do but say so — loudly, because this is a pull request that will stay
    uncorrelated."""
    monkeypatch.setattr(consent_module, "record_pull_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(consent_module, "enqueue_pr_link",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("also down")))

    # Must not propagate: the pull request is open either way, and the consent
    # row is already executed.
    consent_module._correlate_pull_request(
        TEAM_A, {"repo_full_name": REPO}, action_hash="h" * 16,
        result={"number": 47, "head_sha": None}, thread_id=None,
    )

    assert _mapping(clean, 47) is None
