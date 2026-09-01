"""Getting a team's repository onto disk, and keeping it current.

The first thing in Comrade that holds a GitHub credential. Ingest deliberately
held none (pipeline/github.py's header says so) because webhook deliveries
carry their own payload; a clone cannot.

RUNS ON THE EXISTING JOB QUEUE, NOT A NEW LOOP
------------------------------------------------
pipeline/worker.py already has the only complete bounded-retry-with-escalation
loop in the codebase: `lease_expires_at`, `MAX_ATTEMPTS = 3`,
`PermanentJobError` splitting "will never work" from "try again", and reaping
for jobs whose worker died. A clone is exactly that shape — slow, fallible,
sometimes permanently (repo deleted, token revoked) and sometimes not (network
blip) — so it registers a handler rather than growing a second scheduler.

THE TOKEN IS NEVER WRITTEN TO DISK
------------------------------------
The obvious clone URL is `https://x-access-token:TOKEN@github.com/owner/repo`,
and git stores the remote URL in `.git/config`. That would leave a live
credential inside the very tree the agent is about to read. So the remote is
the plain URL and the credential rides on a per-invocation
`-c http.extraHeader`, which is not persisted.

Belt and braces: `.git` is denied to the agent entirely (agent/capability.py),
so even if some future git version cached something there, it is not reachable.
Git history reaches the model through `repo_activity` instead — already built,
already datamarked.

`_token_for` is a seam. Today it hands back the single PAT, which is fine while
the only connected repo is our own and unacceptable the moment a second team
connects one: one identity, one blast radius, no expiry. A GitHub App
installation token — one hour, scoped to that installation's repositories —
replaces the body of this one function and nothing else.
"""
import logging
import subprocess
import time
import uuid
from base64 import b64encode
from pathlib import Path

from pipeline.worker import PermanentJobError, register
from shared.config import settings
from shared.db import Role, connect, team_session
from shared.workspace import (
    ensure_workspace, remove_workspace, repo_checkout, workspaces_root,
)

logger = logging.getLogger(__name__)

#: A clone of a large repository is slow; a hung one must not hold a worker
#: lease until it expires. Well inside worker.py's 30-minute lease.
GIT_TIMEOUT_SECONDS = 600

#: Shallow. The agent reads code, not history — and history is what makes a
#: clone of a mature repository ten times larger than its working tree.
CLONE_DEPTH = "1"


class RepoSyncError(Exception):
    """A repository could not be cloned or refreshed."""


def _token_for(team_id: str, repo_full_name: str) -> str:
    """The credential to fetch this team's repository with.

    The seam the GitHub App slots into. See the module header for why the PAT
    behind it is a stopgap and not an acceptable end state.
    """
    if not settings.github_pat:
        raise PermanentJobError(
            "no GitHub credential is configured, so no repository can be"
            " fetched. Set GITHUB_PAT, or install the GitHub App once it"
            " exists."
        )
    return settings.github_pat


def _auth_header(token: str) -> str:
    basic = b64encode(f"x-access-token:{token}".encode()).decode()
    return f"http.extraHeader=Authorization: Basic {basic}"


def _url_for(repo_full_name: str) -> str:
    """The clone URL. A seam, so the whole module is testable against a local
    git repository with no network and no credential — a clone path verified
    only against a mock is a clone path nobody has run."""
    return f"https://github.com/{repo_full_name}.git"


def _run_git(args: list[str], cwd: Path | None, token: str) -> None:
    """One git invocation, no shell, with the credential passed per-call.

    `check=False` and an explicit raise: git's stderr is the only useful thing
    when a clone fails, and CalledProcessError discards it.
    """
    proc = subprocess.run(  # noqa: S603 - fixed argv, never a shell string
        ["git", "-c", _auth_header(token), *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        # Redacted before it can reach a log, a job row, or a model. The
        # header is in argv, so git echoing a failing command would otherwise
        # print the credential.
        raise RepoSyncError(err.replace(token, "<redacted>")[:800])


def sync_repo(team_id: str, repo_full_name: str) -> Path:
    """Clone the repository if it is absent, fetch and reset it if not.

    Returns the checkout path. `reset --hard` rather than `pull`: the agent's
    edits live in this tree, and a turn must start from what the remote says
    rather than from whatever a previous turn left behind. Anything worth
    keeping has been pushed by then — Phase C's PR action is what keeps it.
    """
    token = _token_for(team_id, repo_full_name)
    ensure_workspace(team_id)
    checkout = repo_checkout(team_id, repo_full_name)
    url = _url_for(repo_full_name)

    try:
        if (checkout / ".git").exists():
            _run_git(["fetch", "--depth", CLONE_DEPTH, "origin"], checkout, token)
            _run_git(["reset", "--hard", "origin/HEAD"], checkout, token)
            _run_git(["clean", "-fd"], checkout, token)
        else:
            _run_git(
                ["clone", "--depth", CLONE_DEPTH, url, str(checkout)], None, token
            )
    except subprocess.TimeoutExpired as exc:
        raise RepoSyncError(
            f"git timed out after {GIT_TIMEOUT_SECONDS}s on {repo_full_name}"
        ) from exc
    except RepoSyncError as exc:
        text = str(exc).lower()
        # Retrying will not help: the repo is gone, private to us, or the
        # credential does not carry it. PermanentJobError is what stops the
        # worker spending its three attempts finding that out.
        if any(
            s in text for s in
            ("repository not found", "authentication failed", "403", "404",
             "could not read username", "permission denied")
        ):
            raise PermanentJobError(
                f"{repo_full_name} cannot be fetched with the configured"
                f" credential: {exc}"
            ) from exc
        raise

    with team_session(Role.PIPELINE, team_id) as conn:
        conn.execute(
            "update public.github_repos set last_cloned_at = now()"
            " where team_id = %s and repo_full_name = %s",
            (team_id, repo_full_name),
        )
    return checkout


def enqueue_sync(team_id: str, repo_full_name: str) -> str:
    """Queue a clone/refresh. Deduped while one is already pending."""
    from psycopg.types.json import Json

    payload = {"repo_full_name": repo_full_name}
    dedupe_key = f"sync:{repo_full_name}"
    with team_session(Role.PIPELINE, team_id) as conn:
        row = conn.execute(
            "insert into public.jobs (team_id, job_type, payload, dedupe_key)"
            " values (%s,'sync_repo',%s,%s)"
            " on conflict (team_id, job_type, dedupe_key)"
            " where dedupe_key is not null and status in ('pending','processing')"
            " do nothing returning id",
            (team_id, Json(payload), dedupe_key),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "select id from public.jobs where team_id=%s and job_type='sync_repo'"
                " and dedupe_key=%s",
                (team_id, dedupe_key),
            ).fetchone()
    return str(row[0])


def handle_sync_repo(team_id: str, payload: dict) -> None:
    name = payload.get("repo_full_name")
    if not name:
        raise PermanentJobError("sync_repo job carries no repo_full_name")
    sync_repo(team_id, name)


register("sync_repo", handle_sync_repo)


# ---------------------------------------------------------------------------
# Lifecycle: reclaiming what no team owns any more
# ---------------------------------------------------------------------------

#: A workspace younger than this is never swept, whatever the database says.
#: The sweep crosses teams on its own connection, so a checkout created
#: moments ago can be invisible to it — without a grace window the reconciler
#: would delete a workspace out from under the turn that just made it.
SWEEP_GRACE_SECONDS = 3600


def _live_team_ids(conn) -> set[str]:
    return {
        str(r[0]) for r in conn.execute("select id from public.teams").fetchall()
    }


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def sweep_orphan_workspaces() -> list[str]:
    """Delete checkouts belonging to teams that no longer exist.

    A RECONCILER, not an event handler, and deliberately. A team can be deleted
    while the worker is down, a `remove` job can fail its three attempts, a
    disk can be restored from a backup taken before a deletion — every one of
    those leaves an orphan that an event-driven cleanup never hears about. A
    sweep converges from any of them.

    Two guards, because a reconciler that deletes is the most dangerous kind of
    code in a system like this:

      * Only directories whose name is a UUID are ever considered. Anything
        else in the workspaces root was not put there by us and is not ours to
        remove.
      * Nothing younger than SWEEP_GRACE_SECONDS is touched, so a workspace
        created after this connection took its snapshot survives to be seen
        next pass.

    Crosses teams, so control-plane (admin), like the job claim and the chat
    sweep.
    """
    root = workspaces_root()
    if not root.exists():
        return []
    with connect(Role.ADMIN) as conn:
        live = _live_team_ids(conn)

    removed: list[str] = []
    cutoff = time.time() - SWEEP_GRACE_SECONDS
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        try:
            team_id = str(uuid.UUID(entry.name))
        except ValueError:
            # Not ours. Something else put it here, and a cleanup routine that
            # deletes what it does not recognise is how a reconciler eats a
            # directory somebody was using.
            continue
        if team_id in live or entry.stat().st_mtime > cutoff:
            continue
        remove_workspace(team_id)
        removed.append(team_id)
    if removed:
        logger.info("removed %d orphaned workspace(s)", len(removed))
    return removed


def enforce_disk_cap() -> list[str]:
    """Evict least-recently-used workspaces until the root is under budget.

    A full disk takes Postgres down with it, so this is availability rather
    than tidiness. Eviction is safe in a way most caches are not: a checkout is
    a copy of something GitHub still has, and the next turn re-clones it.

    ponytail: total size is recomputed by walking the tree. Fine at pilot scale
    (tens of checkouts); if that ever costs real time, cache it per workspace
    on last_cloned_at rather than making the walk cleverer.
    """
    root = workspaces_root()
    if not root.exists():
        return []
    budget = int(settings.comrade_workspaces_max_gb * 1024**3)
    spaces = [d for d in root.iterdir() if d.is_dir()]
    total = sum(_dir_size(d) for d in spaces)
    if total <= budget:
        return []

    evicted: list[str] = []
    # Oldest touched first: the team least likely to be mid-turn.
    for entry in sorted(spaces, key=lambda d: d.stat().st_mtime):
        if total <= budget:
            break
        try:
            team_id = str(uuid.UUID(entry.name))
        except ValueError:
            continue
        total -= _dir_size(entry)
        remove_workspace(team_id)
        evicted.append(team_id)
    logger.warning(
        "workspaces exceeded %.1f GB; evicted %d checkout(s): %s",
        settings.comrade_workspaces_max_gb, len(evicted), ", ".join(evicted),
    )
    return evicted
