"""GitHub webhook deliveries -> `github_activity` rows -> cited wiki facts.

findings §16.5/§16.6, phase-3 plan Task 3: the webhook route (server/app.py)
verifies the signature, resolves which team owns the delivered repo, and
enqueues an `ingest_github` job; this module is everything downstream of
that — the enqueue helpers the route calls, and the worker handler that turns
one delivery into (at most) one `github_activity` row.

Ingest is deliberately dumb: no GitHub API calls (no token is held this
phase), no diffs. It stores what a later stage and a human reader would want —
title, body, author, url, merged/review state, whether the author was a bot,
and the handful of ids that make a row identifiable.

The second half of the file is the compile path, and it is where §22.3's
PROVENANCE RULE lives: only human-verified artifacts become wiki facts. A row
is stored for a bot event (contribution counting is a different question), but
it is filtered out on the way to the model — see _ELIGIBLE_ACTIVITY.
"""
import logging
from typing import Callable

from psycopg.types.json import Json

from pipeline.compiler import apply_compilation, consolidate, extract_candidates
from pipeline.parsers import spotlight
from pipeline.wiki import all_active_pages
from pipeline.worker import register
from shared.db import Role, connect, team_session

logger = logging.getLogger(__name__)


def resolve_team_for_installation(installation_id: int) -> str | None:
    """Which team owns this installation.

    PREFERRED over resolve_team_for_repo, because it cannot be ambiguous:
    github_installations.installation_id is unique across all teams, while two
    teams may legitimately connect the same public repository and the name
    lookup then returns whichever row came back first — silently routing one
    team's activity into another team's wiki.

    Admin for the same reason resolve_team_for_repo is: there is no team yet
    to scope the lookup to.
    """
    with connect(Role.CONTROL) as conn:
        row = conn.execute(
            "select team_id from public.github_installations"
            " where installation_id = %s",
            (installation_id,),
        ).fetchone()
    return str(row[0]) if row else None


def forget_installation(installation_id: int) -> None:
    """The App was uninstalled. Drop the row, and the repositories connected
    through it go with it via the foreign key.

    Without this, an uninstalled App leaves rows whose every sync fails with a
    401 and burns three retries each time — and, worse, leaves a team's UI
    claiming a repository is connected when nothing can read it.
    """
    from shared.github_app import forget

    with connect(Role.CONTROL) as conn:
        conn.execute(
            "delete from public.github_installations where installation_id = %s",
            (installation_id,),
        )
        conn.commit()
    forget(installation_id)


def resolve_team_for_repo(full_name: str) -> str | None:
    """Which team registered this repo, keyed by GitHub's `owner/repo` name.

    The route calls this before it has a team_id — that's the whole problem
    it solves — so, like the queue's job claim in pipeline/worker.py and the
    cross-team scan in pipeline/chat.py's sweep, it runs on the admin
    (control-plane) connection rather than team_session: a lookup across ALL
    teams' repos cannot be scoped to one team's RLS context, because there is
    no team yet to scope it to. Returns None when no team has registered it.
    """
    with connect(Role.CONTROL) as conn:
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
    # `delivery_id` is in the payload as well as the dedupe key: the handler
    # needs it to make the check-result write idempotent across redeliveries,
    # and the dedupe key only stops a SECOND JOB while the first is active.
    payload = {"event": event, "body": body, "delivery_id": delivery_id}
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


def _is_bot(user: dict) -> bool:
    """Is this GitHub actor a machine? findings §22.3.

    GitHub marks app accounts two ways and either is enough: `user.type` is
    "Bot", and the login carries a "[bot]" suffix. Both are checked because
    neither is present on every payload shape — a push's commit author has a
    username and no type at all.

    Recorded at ingestion, NOT acted on here: a bot's event is still a real
    event and `contribution_v` still counts it. The compile path is where the
    provenance rule bites (see _ELIGIBLE_ACTIVITY below).
    """
    return (
        user.get("type") == "Bot"
        or (user.get("login") or "").endswith("[bot]")
    )


# ---------- event -> (node_type, author, occurred_at, stored payload) ----------
# One function per supported event. Each returns the same shape so the
# handler below can stay generic; GitHub sends many event types this
# dictionary does not cover, and that is by design (see handle_github_job).

def _extract_push(body: dict) -> dict:
    # A push can bundle several commits; head_commit is GitHub's own summary
    # of "the one that matters" for exactly this reason. A push with no
    # commits (e.g. a branch delete) has no head_commit — fields go None.
    commit = body.get("head_commit") or {}
    author = commit.get("author") or {}
    title, _, rest = (commit.get("message") or "").partition("\n")
    return {
        "node_type": "commit",
        "author_login": author.get("username"),
        "occurred_at": commit.get("timestamp"),
        "payload": {
            "title": title.strip() or None,
            "body": rest.strip() or None,
            "url": commit.get("url"),
            "number": None,
            "sha": commit.get("id"),
            "merged": None,
            "state": None,
            "bot": _is_bot({"login": author.get("username")}),
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
            "bot": _is_bot(user),
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
            "bot": _is_bot(user),
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
            "bot": _is_bot(user),
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
            "bot": _is_bot(user),
        },
    }


_EXTRACTORS: dict[str, Callable[[dict], dict]] = {
    "push": _extract_push,
    "pull_request": _extract_pull_request,
    "issues": _extract_issue,
    "issue_comment": _extract_issue_comment,
    "pull_request_review": _extract_pull_request_review,
}


#: Deferred, because pipeline.ci imports from here.
def _check_events() -> tuple[str, ...]:
    from pipeline.ci import CHECK_EVENTS

    return CHECK_EVENTS


class _CheckEvents:
    """`event in _CHECK_EVENTS` without importing pipeline.ci at module load."""

    def __contains__(self, event: object) -> bool:
        return event in _check_events()


_CHECK_EVENTS = _CheckEvents()


def _record_check(team_id: str, payload: dict, body: dict) -> None:
    from pipeline.ci import record_check_result

    record_check_result(
        team_id,
        (body.get("repository") or {}).get("full_name") or "",
        payload.get("event") or "",
        body,
        delivery_id=payload.get("delivery_id"),
    )


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
    body = payload.get("body") or {}

    # Which thread's work this result is about. Done HERE rather than in the
    # webhook route: the route passed the delivery id positionally to a
    # keyword-only parameter, so every check event raised TypeError into a
    # broad `except` and was acknowledged with nothing recorded. In the handler
    # a failure leaves the job pending for another attempt, which is the
    # durable path the route was trying and failing to provide.
    #
    # Before the extractor check, because check events are not wiki activity
    # and would return early.
    if event in _CHECK_EVENTS:
        _record_check(team_id, payload, body)

    extractor = _EXTRACTORS.get(event)
    if extractor is None:
        return
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

    # Ambient capture, the way chat's sweep is: nobody asks for a compile, the
    # backlog just gets picked up. `github_activity` is written HERE, by the
    # worker itself, so ingestion is the hook — chat needs a cross-team sweep
    # only because its rows arrive through the API server instead.
    #
    # Best-effort on purpose: the activity row is already committed, so raising
    # would retry this job and write the row twice. A missed enqueue heals
    # itself — the next delivery re-reads everything past the watermark.
    try:
        enqueue_github_compile(team_id)
    except Exception:  # noqa: BLE001 - the ingest row is what mattered
        logger.exception("github compile enqueue failed; activity row is stored")


register("ingest_github", handle_github_job)


# ---------- repository activity -> wiki facts (findings §16.6, §22.3) ----------

MIN_GITHUB_ACTIVITIES = 3  # debounce: roughly one merged PR with its review

# THE PROVENANCE RULE. Linear's 2026 report counted ~2,400 issues/week authored
# by agents against ~2,500 by people, so a compiler reading issue and PR text is
# already half-reading machine output. Only artifacts a HUMAN verified are
# eligible:
#   merge   - a MERGED pull request: a person pressed merge on it
#   review  - a review or review comment left by a person
#   issue   - a person's issue
#   comment - a person's comment on an issue or PR
# The exclusions are the point, not an oversight:
#   pr      - an UNMERGED pull request: nobody has verified its description yet
#   commit  - a push: no review gate, and an agent with a token pushes the same
#             way a person does
#   branch  - no prose to compile
# Bot authorship is filtered in the same fragment; see _is_bot.
_ELIGIBLE_ACTIVITY = ["merge", "review", "issue", "comment"]

_FETCH_ACTIVITY = (
    "select id, node_type, author_github, payload, created_at"
    " from public.github_activity"
    " where team_id = %s and node_type = any(%s)"
    # Two independent bot signals: the flag _is_bot recorded at ingestion, and
    # the login suffix — which also catches rows ingested before that flag
    # existed. Both columns are nullable, hence the coalesces.
    " and coalesce((payload->>'bot')::boolean, false) = false"
    " and coalesce(author_github, '') not ilike '%%[bot]'"
)


def _activity_row(r) -> dict:
    return {
        "id": str(r[0]), "node_type": r[1], "author_github": r[2],
        "payload": r[3] or {}, "created_at": r[4],
    }


def fetch_new_activity(conn, team_id: str, since) -> list[dict]:
    """Human-verified activity newer than the watermark, oldest first.

    The watermark is on created_at (ours, monotonic), not occurred_at
    (GitHub's, nullable): a delivery arriving late with an older occurred_at
    would otherwise fall behind an advanced watermark and never compile.
    """
    rows = conn.execute(
        _FETCH_ACTIVITY + " and created_at > coalesce(%s, '-infinity'::timestamptz)"
        " order by created_at, id",
        (team_id, _ELIGIBLE_ACTIVITY, since),
    ).fetchall()
    return [_activity_row(r) for r in rows]


def fetch_activity_by_id(conn, team_id: str, activity_ids: list[str]) -> list[dict]:
    """Re-fetch an enqueued batch by id, oldest first. Shares _FETCH_ACTIVITY
    with the enqueue path on purpose: the provenance filter is one fragment, so
    a bot row cannot reach the model by way of an id sitting in a job payload."""
    if not activity_ids:
        return []
    rows = conn.execute(
        _FETCH_ACTIVITY + " and id = any(%s::uuid[]) order by created_at, id",
        (team_id, _ELIGIBLE_ACTIVITY, activity_ids),
    ).fetchall()
    return [_activity_row(r) for r in rows]


_ACTIVITY_LABELS = {
    "merge": "merged pull request",
    "issue": "issue",
    "review": "review on",
    "comment": "comment on",
}


def format_activity_transcript(rows: list[dict]) -> str:
    """Numbered transcript, ONE line per activity — the numbering is what
    extraction's source_index refers back to.

    Only `title` and `body` are ever read out of the payload. That whitelist is
    what keeps §16.6's "feed it titles, descriptions and review comments, not
    raw diffs" true regardless of what a future ingest stores alongside them.
    Each activity is collapsed onto a single line so a multi-paragraph PR body
    cannot invent transcript lines for the model to cite.
    """
    lines = []
    for i, r in enumerate(rows):
        payload = r["payload"] or {}
        label = _ACTIVITY_LABELS.get(r["node_type"], r["node_type"])
        if r["node_type"] == "review" and payload.get("state"):
            label = f"review ({payload['state']}) on"
        if payload.get("number"):
            label = f"{label} #{payload['number']}"
        text = " — ".join(
            " ".join(str(field).split())
            for field in (payload.get("title"), payload.get("body"))
            if field and str(field).strip()
        )
        who = r["author_github"] or "unknown"
        lines.append(f"[{i}] {label} by {who}: {text}")
    return "\n".join(lines)


def github_watermark(conn, team_id: str):
    """Latest github_through across done repo compilations (None if never ran).

    Deliberately NOT chat_through: sharing one column would let a repo compile
    advance chat's position past messages nobody compiled, and vice versa.
    """
    return conn.execute(
        "select max(github_through) from public.memory_compilations"
        " where team_id = %s and github_through is not null and status = 'done'",
        (team_id,),
    ).fetchone()[0]


def compile_github_activity(team_id: str, activities: list[dict], through) -> dict:
    """Two-stage compile of a repo batch: extract (github prompt, per-line
    source_index) -> consolidate against the wiki -> apply with per-activity
    citations. Always writes the compilation row, so the watermark advances
    even when the batch produced nothing.

    The transcript is spotlighted before the model sees it: on a public repo,
    PR bodies and review comments are written by strangers (§16.6). Neither LLM
    call happens inside a transaction — same shape as chat and documents.
    """
    transcript = format_activity_transcript(activities)
    marked = spotlight(transcript)
    candidates = extract_candidates(marked, kind="github") if activities else []

    # Map each candidate's transcript line back to the activity it cites;
    # unmappable indices degrade to "no citation", never to a wrong one.
    sources: list[tuple[str, str] | None] = []
    for cand in candidates:
        idx = cand.source_index
        if idx is not None and 0 <= idx < len(activities):
            sources.append(("github", activities[idx]["id"]))
        else:
            sources.append(None)

    with team_session(Role.PIPELINE, team_id) as conn:
        pages = all_active_pages(conn, team_id)

    decisions = consolidate(candidates, pages, len(marked)) if candidates else []

    with team_session(Role.PIPELINE, team_id) as conn:
        return apply_compilation(
            conn, team_id, candidates, decisions, sources,
            trigger="scheduled", github_through=through,
        )


def enqueue_github_compile(
    team_id: str, min_activities: int = MIN_GITHUB_ACTIVITIES
) -> str | None:
    """Debounced enqueue: if >= min_activities eligible rows exist past the
    watermark, queue a compile_github job carrying their ids + new watermark.
    Returns the job id, or None when below threshold.

    Bot and unmerged rows never count towards the threshold — the filter is in
    the same fetch, so a repo full of Dependabot noise never triggers a compile.
    """
    with team_session(Role.PIPELINE, team_id) as conn:
        since = github_watermark(conn, team_id)
        activities = fetch_new_activity(conn, team_id, since)
    if len(activities) < min_activities:
        return None

    payload = {
        "activity_ids": [a["id"] for a in activities],
        "through": max(a["created_at"] for a in activities).isoformat(),
    }
    dedupe_key = f"github:{payload['through']}"
    with team_session(Role.PIPELINE, team_id) as conn:
        row = conn.execute(
            "insert into public.jobs (team_id, job_type, payload, dedupe_key)"
            " values (%s,'compile_github',%s,%s)"
            " on conflict (team_id, job_type, dedupe_key)"
            " where dedupe_key is not null and status in ('pending','processing')"
            " do nothing returning id",
            (team_id, Json(payload), dedupe_key),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "select id from public.jobs where team_id=%s"
                " and job_type='compile_github' and dedupe_key=%s",
                (team_id, dedupe_key),
            ).fetchone()
    return str(row[0])


def handle_github_compile_job(team_id: str, payload: dict) -> None:
    """Worker handler for 'compile_github' jobs."""
    with team_session(Role.PIPELINE, team_id) as conn:
        activities = fetch_activity_by_id(
            conn, team_id, payload.get("activity_ids", [])
        )
    compile_github_activity(team_id, activities, payload.get("through"))


register("compile_github", handle_github_compile_job)
