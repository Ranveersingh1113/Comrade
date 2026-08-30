"""GitHub webhook deliveries -> `github_activity` rows.

findings §16.5/§16.6, phase-3 plan Task 3: the webhook route (server/app.py)
verifies the signature, resolves which team owns the delivered repo, and
enqueues an `ingest_github` job; this module is everything downstream of
that — the enqueue helpers the route calls, and the worker handler that turns
one delivery into (at most) one `github_activity` row.

Read-only and deliberately dumb: no GitHub API calls (no token is held this
phase), no diffs, no compilation. Turning these rows into cited wiki facts —
with the bot/human and merged/unmerged provenance filtering that needs — is
Task 4. This module only has to store what that later stage and a human
reader would want: title, body, author, url, merged/review state, and the
handful of ids that make a row identifiable.
"""
from typing import Callable

from psycopg.types.json import Json

from pipeline.worker import register
from shared.db import Role, connect, team_session


def resolve_team_for_repo(full_name: str) -> str | None:
    """Which team registered this repo, keyed by GitHub's `owner/repo` name.

    The route calls this before it has a team_id — that's the whole problem
    it solves — so, like the queue's job claim in pipeline/worker.py and the
    cross-team scan in pipeline/chat.py's sweep, it runs on the admin
    (control-plane) connection rather than team_session: a lookup across ALL
    teams' repos cannot be scoped to one team's RLS context, because there is
    no team yet to scope it to. Returns None when no team has registered it.
    """
    with connect(Role.ADMIN) as conn:
        row = conn.execute(
            "select team_id from public.github_repos where repo_full_name=%s",
            (full_name,),
        ).fetchone()
    return str(row[0]) if row else None


def enqueue_github_event(
    team_id: str, event: str, delivery_id: str | None, body: dict
) -> str:
    """Queue one verified delivery for ingestion. Returns the job id.

    dedupe_key = the delivery id. GitHub retries a delivery it didn't get a
    2xx for using the SAME X-GitHub-Delivery header, so the partial unique
    index on (team_id, job_type, dedupe_key) already on `jobs`
    (idx_jobs_active_dedupe) makes a retry a no-op instead of a duplicate job
    — same dedupe pattern as enqueue_document/enqueue_chat_compile.

    The full parsed body is queued as-is; it is the HANDLER's job to pick the
    useful subset out of it when it writes github_activity (see module
    docstring). A missing delivery id (never happens with real GitHub, only
    possibly in a test) disables dedup for that one job rather than colliding
    every such job together under a shared '' key.
    """
    payload = {"event": event, "body": body}
    dedupe_key = delivery_id or None
    with team_session(Role.PIPELINE, team_id) as conn:
        row = conn.execute(
            "insert into public.jobs (team_id, job_type, payload, dedupe_key)"
            " values (%s,'ingest_github',%s,%s)"
            " on conflict (team_id, job_type, dedupe_key)"
            " where dedupe_key is not null and status in ('pending','processing')"
            " do nothing returning id",
            (team_id, Json(payload), dedupe_key),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "select id from public.jobs where team_id=%s"
                " and job_type='ingest_github' and dedupe_key=%s",
                (team_id, dedupe_key),
            ).fetchone()
    return str(row[0])


# ---------- event -> (node_type, author, occurred_at, stored payload) ----------
# One function per supported event. Each returns the same shape so the
# handler below can stay generic; GitHub sends many event types this
# dictionary does not cover, and that is by design (see handle_github_job).

def _extract_push(body: dict) -> dict:
    # A push can bundle several commits; head_commit is GitHub's own summary
    # of "the one that matters" for exactly this reason. A push with no
    # commits (e.g. a branch delete) has no head_commit — fields go None.
    commit = body.get("head_commit") or {}
    title, _, rest = (commit.get("message") or "").partition("\n")
    return {
        "node_type": "commit",
        "author_login": (commit.get("author") or {}).get("username"),
        "occurred_at": commit.get("timestamp"),
        "payload": {
            "title": title.strip() or None,
            "body": rest.strip() or None,
            "url": commit.get("url"),
            "number": None,
            "sha": commit.get("id"),
            "merged": None,
            "state": None,
        },
    }


def _extract_pull_request(body: dict) -> dict:
    pr = body.get("pull_request") or {}
    merged = pr.get("merged")
    user = pr.get("user") or {}
    return {
        "node_type": "merge" if merged else "pr",
        "author_login": user.get("login"),
        "occurred_at": pr.get("merged_at") or pr.get("updated_at") or pr.get("created_at"),
        "payload": {
            "title": pr.get("title"),
            "body": pr.get("body"),
            "url": pr.get("html_url"),
            "number": pr.get("number"),
            "sha": None,
            "merged": merged,
            "state": pr.get("state"),
        },
    }


def _extract_issue(body: dict) -> dict:
    issue = body.get("issue") or {}
    user = issue.get("user") or {}
    return {
        "node_type": "issue",
        "author_login": user.get("login"),
        "occurred_at": issue.get("updated_at") or issue.get("created_at"),
        "payload": {
            "title": issue.get("title"),
            "body": issue.get("body"),
            "url": issue.get("html_url"),
            "number": issue.get("number"),
            "sha": None,
            "merged": None,
            "state": issue.get("state"),
        },
    }


def _extract_issue_comment(body: dict) -> dict:
    # Fires for comments on both issues and PRs; GitHub calls the parent
    # "issue" either way. Its title rides along so the row is readable on its
    # own ("comment on <title>") without a join back to the parent.
    comment = body.get("comment") or {}
    parent = body.get("issue") or {}
    user = comment.get("user") or {}
    return {
        "node_type": "comment",
        "author_login": user.get("login"),
        "occurred_at": comment.get("updated_at") or comment.get("created_at"),
        "payload": {
            "title": parent.get("title"),
            "body": comment.get("body"),
            "url": comment.get("html_url"),
            "number": parent.get("number"),
            "sha": None,
            "merged": None,
            "state": None,
        },
    }


def _extract_pull_request_review(body: dict) -> dict:
    review = body.get("review") or {}
    pr = body.get("pull_request") or {}
    user = review.get("user") or {}
    return {
        "node_type": "review",
        "author_login": user.get("login"),
        "occurred_at": review.get("submitted_at"),
        "payload": {
            "title": pr.get("title"),
            "body": review.get("body"),
            "url": review.get("html_url"),
            "number": pr.get("number"),
            "sha": None,
            "merged": None,
            "state": review.get("state"),  # approved | changes_requested | commented
        },
    }


_EXTRACTORS: dict[str, Callable[[dict], dict]] = {
    "push": _extract_push,
    "pull_request": _extract_pull_request,
    "issues": _extract_issue,
    "issue_comment": _extract_issue_comment,
    "pull_request_review": _extract_pull_request_review,
}


def handle_github_job(team_id: str, payload: dict) -> None:
    """Worker handler for 'ingest_github' jobs.

    An event type not in _EXTRACTORS is ignored deliberately, not an error:
    GitHub sends far more event types than this codebase acts on (stars,
    forks, releases, ...), and raising on every one of them would fill the
    queue with permanent failures for events that were never a bug.

    The repo is re-resolved here, inside team_session(PIPELINE, team_id),
    rather than trusting the id the admin-role route lookup saw — RLS is the
    authorization layer, so the row this handler is allowed to see is the
    proof the repo still belongs to this team, not the route's earlier say-so.
    """
    event = payload.get("event")
    extractor = _EXTRACTORS.get(event)
    if extractor is None:
        return
    body = payload.get("body") or {}
    extracted = extractor(body)
    full_name = (body.get("repository") or {}).get("full_name")
    author_login = extracted["author_login"]

    with team_session(Role.PIPELINE, team_id) as conn:
        repo = conn.execute(
            "select id from public.github_repos where repo_full_name=%s",
            (full_name,),
        ).fetchone()
        if repo is None:
            return  # repo disconnected between delivery and processing; drop

        author_user_id = None
        if author_login:
            # profiles.github_username -> author_user_id. RLS (pl_profiles)
            # only lets this role see a profile that is a member of THIS
            # team, so a login belonging to someone outside team_id resolves
            # to NULL here too — not just an unmapped login. An unmapped
            # author is normal (most contributors never link a GitHub
            # username), not an error.
            match = conn.execute(
                "select id from public.profiles where github_username=%s",
                (author_login,),
            ).fetchone()
            author_user_id = match[0] if match else None

        conn.execute(
            "insert into public.github_activity"
            " (team_id, repo_id, node_type, author_github, author_user_id,"
            "  payload, occurred_at)"
            " values (%s,%s,%s,%s,%s,%s,%s)",
            (
                team_id, repo[0], extracted["node_type"], author_login,
                author_user_id, Json(extracted["payload"]), extracted["occurred_at"],
            ),
        )


register("ingest_github", handle_github_job)
