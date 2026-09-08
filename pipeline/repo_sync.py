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

THE CREDENTIAL BELONGS TO THE TEAM, NOT TO US
-----------------------------------------------
`_token_for(team_id, repo_full_name)` once ignored both arguments and returned
one process-wide PAT — one identity, one blast radius, no expiry — so a team's
checkout was fetched with a credential scoped to every repository its owner
could reach. It now mints a GitHub App installation token: one hour, scoped by
GitHub to the repositories that installation was granted. The PAT survives only
as a single-tenant local escape hatch and refuses itself once a second team has
connected anything.
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
from shared.github_app import GitHubAppError, installation_token
from shared.workspace import (
    ensure_workspace, force_rmtree, remove_workspace, repo_checkout,
    thread_checkout, workspaces_root,
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


def _installation_for(team_id: str, repo_full_name: str) -> int | None:
    """Which installation reaches this team's copy of this repository.

    Joined rather than looked up by name: `github_repos.installation_id` is
    constrained by RLS to an installation the SAME team owns, so reading it
    back through team_session is what carries that guarantee into the token we
    then mint. A lookup keyed on repo_full_name alone would find another
    team's row, and two teams may legitimately connect the same public repo.
    """
    with team_session(Role.PIPELINE, team_id) as conn:
        row = conn.execute(
            "select r.installation_id from public.github_repos r"
            " join public.github_installations i"
            "   on i.installation_id = r.installation_id"
            " where r.team_id = %s and r.repo_full_name = %s",
            (team_id, repo_full_name),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _pat_is_still_single_tenant() -> bool:
    """Whether the local PAT can be used without becoming a cross-tenant hole.

    The PAT is scoped to everything its owner can reach, so the moment a
    SECOND team would be reached with it, it stops being "my own credential for
    my own repo" and becomes "one team's agent holding another team's access".
    Counted rather than assumed, and refused rather than warned about: a
    warning in a log is not a boundary.

    Counts teams that would FALL THROUGH TO THE PAT, not teams with a
    repository. A team whose repositories all have an installation never
    reaches this credential, so counting them would have one team's completed
    migration to the App switch off another team's local development — a
    refusal with no one on the other side of it. Found when a real connected
    repository in a developer's own database started failing a test about a
    credential it does not use.
    """
    with connect(Role.CONTROL) as conn:
        teams = conn.execute(
            "select count(distinct team_id) from public.github_repos"
            " where installation_id is null"
        ).fetchone()[0]
    return teams <= 1


def _token_for(team_id: str, repo_full_name: str) -> str:
    """The credential to fetch THIS team's copy of THIS repository with.

    Both arguments are now load-bearing. They were not: this returned one
    process-wide PAT for every caller, so a team's checkout was fetched with a
    credential scoped to every repository its owner could reach. See the
    module header and 20260902120000_github_app.sql.
    """
    installation_id = _installation_for(team_id, repo_full_name)
    if installation_id is not None:
        try:
            return installation_token(installation_id)
        except GitHubAppError as exc:
            # A revoked or suspended installation is not worth three attempts:
            # nothing about retrying makes a removed grant come back, and the
            # team has to reinstall the App either way.
            raise PermanentJobError(str(exc)) from exc

    if settings.github_pat:
        if not _pat_is_still_single_tenant():
            raise PermanentJobError(
                f"{repo_full_name} is connected without a GitHub App"
                " installation, and the local GITHUB_PAT cannot be used once"
                " more than one team has connected a repository — it is scoped"
                " to everything its owner can reach, including the other"
                " team's. Install the GitHub App for this team."
            )
        return settings.github_pat

    raise PermanentJobError(
        f"no GitHub credential reaches {repo_full_name}. Install the Comrade"
        " GitHub App on the account that owns it and connect the repository"
        " again."
    )


#: Flags on EVERY git invocation, not a tuning knob.
#:
#: `core.fsmonitor` starts `git fsmonitor--daemon --detach`, a background
#: process that deliberately OUTLIVES the git command that started it and
#: holds ~37MB of commit charge with 8 IPC threads. One per repository. It is
#: a real win on a large checkout a developer works in all day; it is pure
#: leak for a tree we `reset --hard` every turn and delete when the team
#: leaves.
#:
#: 418 of them accumulated on this machine in two hours of running the test
#: suite -- 15GB of commit charge -- and took the box to within half a
#: gigabyte of its commit limit, where an unrelated pytest run died with a
#: MemoryError that pointed nowhere near git. A server hosting many teams
#: accumulates one per checkout and never gives it back.
#:
#: `protocol.version=2` is unrelated and free: fewer refs on the wire for the
#: shallow fetches this module does constantly.
GIT_FLAGS = ("-c", "core.fsmonitor=false", "-c", "protocol.version=2")


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
        ["git", *GIT_FLAGS, "-c", _auth_header(token), *args],
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
    return proc.stdout


def _git_local(args: list[str], cwd: Path) -> str:
    """A git command against a local tree. No credential, by construction.

    Separate from `_run_git` so the type system of the file says what the
    security note says: making a thread's working tree touches no remote, so
    nothing here should be able to mint or leak a token.
    """
    proc = subprocess.run(  # noqa: S603 - fixed argv, never a shell string
        ["git", *GIT_FLAGS, *args],
        cwd=str(cwd), capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise RepoSyncError((proc.stderr or proc.stdout or "").strip()[:800])
    return proc.stdout


def ensure_thread_checkout(
    team_id: str, thread_id: str, repo_full_name: str,
) -> Path:
    """This thread's own working tree, created on first use.

    A `git worktree` of the team's checkout: separate files, one shared object
    database, no second clone and no network. The team checkout stays the
    mirror that fetches and pushes; nobody edits it any more.

    Idempotent — a second turn in the same thread continues in the same tree,
    which is what makes work survive a restart and what stops `sync_repo`'s
    `reset --hard` from deleting a member's unproposed change.
    """
    tree = thread_checkout(team_id, thread_id, repo_full_name)
    if (tree / ".git").exists():
        return tree
    base = repo_checkout(team_id, repo_full_name)
    if not (base / ".git").exists():
        # Deliberately NOT a clone. This is reached from read tools inside a
        # turn, and syncing here would put a credentialled network fetch on the
        # path of `repo_read`. Hand back the path that does not exist; every
        # caller already has a "this repository is not checked out" message,
        # and cloning is the sync JOB's work.
        return tree
    tree.parent.mkdir(parents=True, exist_ok=True)
    # A worktree whose directory was deleted stays REGISTERED, and git then
    # refuses to add it back under the same path. Pruning first makes a
    # cleaned-up disk and git's own bookkeeping agree.
    _git_local(["worktree", "prune"], base)
    _git_local(["worktree", "add", "--detach", str(tree), "HEAD"], base)
    return tree


def default_branch(team_id: str, repo_full_name: str) -> str:
    """The branch the remote's HEAD points at. ASKED, never assumed.

    Two functions used to guess this and both were wrong in the same way, so
    the answer lives in one place now.

      * `sync_repo` reset to `origin/HEAD`. That ref is written by `git clone`
        and never by `git fetch` — so a checkout cloned while the repository
        was still empty has no `origin/HEAD` and can NEVER recover on its own.
        Every later turn fails on a repo that has been fine for weeks.
      * `open_pull_request` fell back to the literal string "main", so a repo
        whose default branch is `master` — or one with no commits at all —
        failed with "couldn't find remote ref main", a message about the wrong
        thing entirely.

    `ls-remote --symref` asks the remote, which is authoritative, needs no
    local ref, and works on the shallow clone `sync_repo` makes. A repository
    with no HEAD has no commits, and that is worth saying out loud rather than
    guessing a branch name into a confusing failure three functions away.
    """
    checkout = repo_checkout(team_id, repo_full_name)
    out = _run_git(
        ["ls-remote", "--symref", "origin", "HEAD"],
        checkout,
        _token_for(team_id, repo_full_name),
    )
    for line in out.splitlines():
        if line.startswith("ref:"):
            return line.split()[1].rsplit("/", 1)[-1]
    raise RepoSyncError(
        f"{repo_full_name} has no commits yet, so there is no branch to work"
        " from. Push an initial commit first — GitHub cannot open a pull"
        " request against an empty repository either."
    )


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
            base = default_branch(team_id, repo_full_name)
            _run_git(
                ["fetch", "--depth", CLONE_DEPTH, "origin", base], checkout, token
            )
            _run_git(["reset", "--hard", "FETCH_HEAD"], checkout, token)
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
            # `subject` repeats the repository name out of the payload so
            # the two control-plane readers — sweep_stale_checkouts below and
            # the connect screen's failure list — can find it without being
            # given the payload, which also carries whole webhook bodies.
            "insert into public.jobs (team_id, job_type, payload, dedupe_key,"
            " subject) values (%s,'sync_repo',%s,%s,%s)"
            " on conflict (team_id, job_type, dedupe_key)"
            " where dedupe_key is not null and status in ('pending','processing')"
            " do nothing returning id",
            (team_id, Json(payload), dedupe_key, repo_full_name),
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
    # 🔴 NO AUTOMATIC DEPENDENCY INSTALL HERE. This called
    # _install_dependencies, and the trigger was wrong.
    #
    # The privilege split was right: installing reaches the network, running
    # the team's code does not, and the model cannot ask for either. What that
    # reasoning missed is a second threat entirely. Connecting a repository is
    # a READ consent in a member's head — "Comrade can see our code". Running
    # its manifest on every sync silently turns that into "Comrade may execute
    # this repository's dependency graph, with egress, unattended", because
    # `pip install` runs setup.py and a build hook runs whatever it likes.
    #
    # Nobody consented to the second thing, and in a product whose entire
    # thesis is that actions are proposed and approved, an execute-with-network
    # capability arriving as a side effect of a checkbox is the one shape that
    # cannot be defended.
    #
    # The install path stays (pipeline/repo_deps.py). What it needs is an
    # explicit per-repository opt-in, an environment whose status a member can
    # see, and a cache key that includes the commit — see that module's header.


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


def _live_checkouts(conn) -> set[tuple[str, str]]:
    """(team_id, checkout directory name) for every repository still connected.

    Disconnecting a repository is a row delete the frontend does directly under
    RLS, so nothing server-side hears about it. The checkout would otherwise
    sit on disk indefinitely holding a copy of code the team has told us to
    stop reading — which is the part that matters, more than the bytes.
    """
    return {
        (str(team_id), repo_checkout(str(team_id), name).name)
        for team_id, name in conn.execute(
            "select team_id, repo_full_name from public.github_repos"
        ).fetchall()
    }


def _dir_size(path: Path) -> tuple[int, int]:
    """Bytes under a path, and how many files could not be measured.

    🔴 Failures are COUNTED, not swallowed. A stat that raises — a file deleted
    mid-walk, a permission error, a broken link — used to abort the whole
    measurement; treating it as zero would be worse still, because an
    undercount means never evicting and a full disk takes Postgres with it.
    The caller refuses to act on an incomplete number rather than guessing.
    """
    total = 0
    failed = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            failed += 1
    return total, failed


def _teams_with_live_processes() -> set[str]:
    """Teams that currently have a sandbox process running.

    Their workspace is mounted into a container right now: evicting it deletes
    files out from under a running server, which surfaces to the member as
    their preview breaking for no stated reason.
    """
    from shared.db import Role as _Role, connect as _connect

    try:
        with _connect(_Role.CONTROL) as conn:
            conn.autocommit = True
            rows = conn.execute(
                "select distinct team_id::text from public.sandbox_processes"
                " where state in ('starting','running')"
            ).fetchall()
        return {row[0] for row in rows}
    except Exception:  # noqa: BLE001 - a lookup failure must not license eviction
        logger.exception("could not list live processes; refusing to evict")
        raise


def _has_uncommitted_work(space: Path) -> bool:
    """Whether any checkout under this workspace has changes nobody proposed.

    A thread's worktree holds work in progress. It is NOT recoverable from
    GitHub — that is the whole point of it — so the "eviction is safe because a
    checkout is a copy" reasoning does not apply to a dirty one.
    """
    for git_dir in list(space.rglob(".git"))[:50]:
        checkout = git_dir.parent
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv
                ["git", *GIT_FLAGS, "status", "--porcelain"],
                cwd=checkout, capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            # Cannot tell: assume there is work rather than deleting it.
            return True
        if proc.returncode == 0 and proc.stdout.strip():
            return True
    return False


#: How old a checkout may be before the reconciler refreshes it. A push
#: webhook enqueues a sync immediately, so this is the backstop for the cases
#: a webhook never arrives for: a repository connected before the App's
#: webhook was reachable, a delivery GitHub gave up retrying, a worker that
#: was down when the push happened.
CHECKOUT_STALE_SECONDS = 900

#: Per tick. A team connecting twenty repositories at once should not enqueue
#: twenty clones in one breath and starve every other job behind them; the
#: next tick takes the next batch.
SYNC_BATCH = 20

#: 🔴 How long to leave a FAILED sync alone. Without this the worker spins.
#:
#: tick() drains the queue and then sweeps. A sync that fails is no longer
#: 'pending', so enqueue_sync's dedupe stops applying and the sweep queues it
#: again immediately — and because tick() processed something, main() skips its
#: sleep and goes straight round again. One repository nobody can clone
#: therefore pins a CPU at full speed and fills the jobs table: four failed
#: attempts inside one second, observed, from a single misconfigured repo.
#:
#: A reconciler must converge, and one that retries a permanent failure as fast
#: as the machine allows converges on nothing but heat.
SYNC_RETRY_BACKOFF_SECONDS = 300


def sweep_stale_checkouts() -> list[str]:
    """Clone what has been connected but never fetched, and refresh what is old.

    🔴 WITHOUT THIS, CONNECTING A REPOSITORY DID NOTHING. `enqueue_sync` was
    written, tested and never called: the row went in, no clone was ever
    queued, `last_cloned_at` stayed null — and `connected_repo` filters on
    exactly that column, so the agent went on answering "no repository is
    connected" to a team looking at their repository listed as connected. Every
    tool in Phase B through D was unreachable through the product.

    A RECONCILER rather than a hook on the insert, for the reason the orphan
    sweep gives: connecting is a row write the frontend makes straight to
    Supabase under RLS, so there is no server-side event to hang a hook on, and
    a reconciler converges whether the row arrived through the UI, through
    psql, or while the worker was down.
    """
    with connect(Role.CONTROL) as conn:
        rows = conn.execute(
            "select r.team_id, r.repo_full_name from public.github_repos r"
            " where (r.last_cloned_at is null"
            "        or r.last_cloned_at < now() - make_interval(secs => %s))"
            # Leave a recent failure alone. See SYNC_RETRY_BACKOFF_SECONDS:
            # without this the sweep re-queues a failing clone the instant it
            # fails, and the worker never sleeps.
            "   and not exists ("
            "     select 1 from public.jobs j"
            "      where j.team_id = r.team_id"
            "        and j.job_type = 'sync_repo'"
            "        and j.status = 'failed'"
            "        and j.subject = r.repo_full_name"
            "        and j.finished_at > now() - make_interval(secs => %s))"
            " order by r.last_cloned_at nulls first limit %s",
            (CHECKOUT_STALE_SECONDS, SYNC_RETRY_BACKOFF_SECONDS, SYNC_BATCH),
        ).fetchall()

    queued: list[str] = []
    for team_id, repo_full_name in rows:
        try:
            enqueue_sync(str(team_id), repo_full_name)
            queued.append(f"{team_id}/{repo_full_name}")
        except Exception:  # noqa: BLE001 - one bad row must not stop the rest
            logger.exception("could not queue a sync for %s", repo_full_name)
    if queued:
        logger.info("queued %d repository sync(s)", len(queued))
    return queued


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
    with connect(Role.CONTROL) as conn:
        live = _live_team_ids(conn)
        connected = _live_checkouts(conn)

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
        if team_id not in live:
            if entry.stat().st_mtime > cutoff:
                continue
            remove_workspace(team_id)
            removed.append(team_id)
            continue

        # The team is live, but individual repositories inside it may have been
        # disconnected. Same two guards as above, one level down: only
        # directories we would have created, and nothing inside the grace
        # window — a checkout being cloned right now has no github_repos row
        # visible to the snapshot this sweep took.
        for checkout in entry.iterdir():
            if not checkout.is_dir():
                continue
            if (team_id, checkout.name) in connected:
                continue
            if checkout.stat().st_mtime > cutoff:
                continue
            force_rmtree(checkout)
            removed.append(f"{team_id}/{checkout.name}")
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
    measured = [_dir_size(d) for d in spaces]
    total = sum(size for size, _ in measured)
    unmeasured = sum(failed for _, failed in measured)
    if unmeasured:
        # Reported, and it does NOT read as "under budget". An undercount that
        # silently passes is how a disk fills while a check says it is fine.
        logger.warning(
            "%d file(s) under the workspaces root could not be measured;"
            " the total of %.1f GB is a floor, not the real usage",
            unmeasured, total / 1024**3,
        )
    if total <= budget and not unmeasured:
        return []

    busy = _teams_with_live_processes()
    evicted: list[str] = []
    skipped: list[str] = []
    # Oldest touched first: the team least likely to be mid-turn.
    for entry in sorted(spaces, key=lambda d: d.stat().st_mtime):
        if total <= budget:
            break
        try:
            team_id = str(uuid.UUID(entry.name))
        except ValueError:
            continue
        if team_id in busy:
            # Its workspace is mounted into a running container right now.
            skipped.append(f"{team_id} (process running)")
            continue
        if _has_uncommitted_work(entry):
            # Not recoverable from GitHub, which is exactly what makes the
            # usual "a checkout is just a copy" argument not apply.
            skipped.append(f"{team_id} (uncommitted work)")
            continue
        total -= _dir_size(entry)[0]
        remove_workspace(team_id)
        evicted.append(team_id)
    if skipped:
        logger.warning(
            "kept %d workspace(s) that were not safe to evict: %s",
            len(skipped), ", ".join(skipped),
        )
    logger.warning(
        "workspaces exceeded %.1f GB; evicted %d checkout(s): %s",
        settings.comrade_workspaces_max_gb, len(evicted), ", ".join(evicted),
    )
    return evicted
