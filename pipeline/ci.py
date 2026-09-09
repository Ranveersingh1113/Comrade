"""Getting CI results back to the work that caused them.

🔴 THE DEFECT. A pull request Comrade opened returned its number to the
consent flow and was then forgotten — no row, no thread mapping, nothing. So
when GitHub reported that the checks had failed, there was no way to say WHOSE
work had failed: the delivery was ingested as repository activity for the wiki
and the thread that produced the change never heard about it. A member had to
go and look.
"""
import logging

from psycopg.types.json import Json  # noqa: F401 - kept for symmetry with siblings

from pipeline.worker import register
from shared.db import Role, team_session

logger = logging.getLogger(__name__)

#: GitHub events that carry a check result.
CHECK_EVENTS = ("check_run", "check_suite", "workflow_run")

#: Terminal statuses. A check that is still running is not news for a thread —
#: telling somebody their work is "queued" is noise, and the same delivery will
#: arrive again with an answer.
TERMINAL = ("completed",)


def record_pull_request(
    team_id: str, repo_full_name: str, pr_number: int, branch: str,
    *, thread_id: str | None, action_hash: str | None, head_sha: str | None,
) -> None:
    """Remember that this pull request is this thread's work.

    Upsert on (team, repo, number), because opening a pull request is
    deliberately safe to repeat: `open_pull_request` returns an already-open
    PR rather than a second one, so this has to converge the same way.
    """
    with team_session(Role.EXECUTOR, team_id) as conn:
        conn.execute(
            "insert into public.github_pull_requests"
            " (team_id, repo_full_name, pr_number, branch, thread_id,"
            "  action_hash, head_sha) values (%s,%s,%s,%s,%s,%s,%s)"
            " on conflict (team_id, repo_full_name, pr_number) do update"
            "   set branch = excluded.branch,"
            "       thread_id = coalesce(excluded.thread_id,"
            "                            public.github_pull_requests.thread_id),"
            "       head_sha = coalesce(excluded.head_sha,"
            "                           public.github_pull_requests.head_sha)",
            (team_id, repo_full_name, pr_number, branch, thread_id,
             action_hash, head_sha),
        )


def enqueue_pr_link(
    team_id: str, repo_full_name: str, pr_number: int, branch: str,
    *, thread_id: str | None, action_hash: str | None, head_sha: str | None,
) -> str | None:
    """Queue the one row a completed pull request still needs.

    🔴 (fix.md F41) The correlation write used to fail into a log line that
    claimed "a later attempt returns the same PR and writes the mapping then".
    There is no later attempt: `execute_consent` moves the row to `executed`
    with a CAS BEFORE running the executor, so a retry returns `noop` and the
    executor never runs again. The pull request stayed open with no thread, so
    every CI check for it had nowhere to report and the people who asked for
    the work heard nothing.

    The intent goes on the JOB QUEUE rather than into a table of its own: that
    queue already has attempts, backoff, and a dedupe key, which is the whole
    of what a repair intent needs. Deduped on the pull request, so a consent
    flow that fails repeatedly leaves one repair rather than a heap.
    """
    from psycopg.types.json import Json

    payload = {
        "repo_full_name": repo_full_name, "pr_number": int(pr_number),
        "branch": branch, "thread_id": thread_id,
        "action_hash": action_hash, "head_sha": head_sha,
    }
    with team_session(Role.PIPELINE, team_id) as conn:
        row = conn.execute(
            "insert into public.jobs (team_id, job_type, payload, dedupe_key)"
            " values (%s,'link_pull_request',%s,%s)"
            " on conflict (team_id, job_type, dedupe_key)"
            " where dedupe_key is not null and status in ('pending','processing')"
            " do nothing returning id",
            (team_id, Json(payload), f"{repo_full_name}#{pr_number}"),
        ).fetchone()
    return str(row[0]) if row else None


def handle_pr_link(team_id: str, payload: dict) -> None:
    """Write the mapping a completed pull request is missing.

    BOOKKEEPING ONLY. It opens nothing and asks nobody: the publication
    already happened and the consent row is already `executed`, so the repair
    must not need another approval or a second call to GitHub (fix.md F41).
    `record_pull_request` upserts, so a queue that delivers this twice
    converges instead of duplicating.
    """
    record_pull_request(
        team_id,
        payload["repo_full_name"],
        int(payload["pr_number"]),
        payload["branch"],
        thread_id=payload.get("thread_id"),
        action_hash=payload.get("action_hash"),
        head_sha=payload.get("head_sha"),
    )


# Registered at import, like every sibling handler. `pipeline/worker.py`
# imports this module for exactly that reason.
register("link_pull_request", handle_pr_link)


def _pull_request_for(conn, team_id: str, repo: str, branch: str | None,
                      pr_numbers: list[int]):
    """The PR a check belongs to, by number if the event gives one, else by
    branch. Both are supplied by GitHub; neither is always present."""
    if pr_numbers:
        row = conn.execute(
            "select id, thread_id, head_sha from public.github_pull_requests"
            " where team_id=%s and repo_full_name=%s and pr_number = any(%s)",
            (team_id, repo, pr_numbers),
        ).fetchone()
        if row:
            return row
    if branch:
        return conn.execute(
            "select id, thread_id, head_sha from public.github_pull_requests"
            " where team_id=%s and repo_full_name=%s and branch=%s"
            " order by opened_at desc limit 1",
            (team_id, repo, branch),
        ).fetchone()
    return None


def _details(event: str, body: dict) -> dict | None:
    """The parts of a check delivery this system cares about, or None."""
    if event == "check_run":
        run = body.get("check_run") or {}
        return {
            "name": run.get("name") or "check",
            "status": run.get("status") or "",
            "conclusion": run.get("conclusion"),
            "url": run.get("html_url"),
            "sha": run.get("head_sha"),
            "branch": (run.get("check_suite") or {}).get("head_branch"),
            "prs": [p.get("number") for p in (run.get("pull_requests") or [])
                    if p.get("number")],
        }
    if event == "check_suite":
        suite = body.get("check_suite") or {}
        return {
            "name": "check suite",
            "status": suite.get("status") or "",
            "conclusion": suite.get("conclusion"),
            "url": suite.get("url"),
            "sha": suite.get("head_sha"),
            "branch": suite.get("head_branch"),
            "prs": [p.get("number") for p in (suite.get("pull_requests") or [])
                    if p.get("number")],
        }
    if event == "workflow_run":
        run = body.get("workflow_run") or {}
        return {
            "name": run.get("name") or "workflow",
            "status": run.get("status") or "",
            "conclusion": run.get("conclusion"),
            "url": run.get("html_url"),
            "sha": run.get("head_sha"),
            "branch": run.get("head_branch"),
            "prs": [p.get("number") for p in (run.get("pull_requests") or [])
                    if p.get("number")],
        }
    return None


def record_check_result(
    team_id: str, repo_full_name: str, event: str, body: dict,
    *, delivery_id: str | None,
) -> str | None:
    """Persist one check result against the work it belongs to.

    Returns the row id, or None when the delivery is not a check, is not
    terminal, or names no pull request this team opened.
    """
    details = _details(event, body)
    if details is None or details["status"] not in TERMINAL:
        return None
    sha = details["sha"]
    if not sha:
        return None

    with team_session(Role.PIPELINE, team_id) as conn:
        pr = _pull_request_for(
            conn, team_id, repo_full_name, details["branch"], details["prs"],
        )
        pull_request_id = pr[0] if pr else None
        thread_id = pr[1] if pr else None
        # 🔴 A result about a commit the branch has moved past must not drive a
        # decision about work that has since changed — the failure it reports
        # may already be fixed. Kept rather than dropped: it is real history,
        # and an audit that quietly omits the failures nobody acted on is not
        # an audit.
        stale = bool(pr and pr[2] and pr[2] != sha)
        row = conn.execute(
            "insert into public.github_check_results"
            " (team_id, repo_full_name, pull_request_id, thread_id, head_sha,"
            "  check_name, status, conclusion, details_url, stale, delivery_id)"
            " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " on conflict (delivery_id) do nothing returning id",
            (team_id, repo_full_name, pull_request_id, thread_id, sha,
             details["name"], details["status"], details["conclusion"],
             details["url"], stale, delivery_id),
        ).fetchone()
    if row is None:
        # A redelivery. The job queue already dedupes an ACTIVE delivery, but
        # only while the job is pending or processing — a manual redelivery
        # from GitHub's UI after the first finished would otherwise land twice.
        logger.info("github: delivery %s already recorded", delivery_id)
        return None
    return str(row[0])
