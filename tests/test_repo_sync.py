"""Getting a team's repository onto disk.

Two halves, deliberately. The policy half (what is permanent, what is
retryable, what is deduped, what is redacted) runs against a real local git
repository — no network, no token, no mocks — because a clone that is only ever
tested against a fake is a clone nobody has run. The GitHub half is asserted on
the arguments we would send rather than by talking to github.com.

The one that matters most is test_the_token_is_never_written_into_the_checkout.
The obvious clone URL embeds the credential and git persists the remote URL in
`.git/config`, which would leave a live token inside the exact tree the agent
is about to read.
"""
import os
import subprocess
import time
from pathlib import Path

import psycopg
import pytest

from pipeline.repo_sync import (
    GIT_TIMEOUT_SECONDS, SWEEP_GRACE_SECONDS, RepoSyncError, _auth_header, _run_git, enqueue_sync,
    SYNC_RETRY_BACKOFF_SECONDS, handle_sync_repo, sweep_orphan_workspaces,
    sweep_stale_checkouts, sync_repo,
)
from pipeline.worker import PermanentJobError
from shared.config import settings
from shared.workspace import repo_checkout
from tests._seed import A1, TEAM_A


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def workspaces(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "shared.config.settings.comrade_workspaces_root", str(tmp_path / "ws")
    )
    # The single-tenant PAT guard counts repositories across ALL teams, which
    # is the point of it — but it therefore reads rows this suite does not own.
    # A developer who has connected their own repository locally is a second
    # team, and every git test here would start failing on a credential
    # decision none of them are about.
    #
    # Neutralised rather than worked around: which credential _token_for picks,
    # and when it refuses the PAT, is tested properly in test_github_app.py
    # against rows that suite creates and deletes. These tests are about git.
    monkeypatch.setattr(
        "pipeline.repo_sync._pat_is_still_single_tenant", lambda: True
    )
    return tmp_path


@pytest.fixture
def origin(tmp_path):
    """A real git repository to clone, on disk, with no network involved."""
    repo = tmp_path / "origin"
    repo.mkdir()
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", *a], cwd=repo, check=True, capture_output=True
    )
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@test.dev")
    run("config", "user.name", "Test")
    (repo / "app.py").write_text("print('hello')\n")
    (repo / ".env").write_text("SECRET=1\n")
    run("add", "-A")
    run("commit", "-q", "-m", "initial")
    return repo


# ---------------------------------------------------------------------------
# The credential
# ---------------------------------------------------------------------------

def test_the_token_is_never_written_into_the_checkout(workspaces, origin, monkeypatch):
    """🔴 The failure this module is shaped around.

    `https://x-access-token:TOKEN@github.com/owner/repo` is the obvious clone
    URL, and git stores the remote URL in `.git/config`. That leaves a live
    credential inside the very tree the agent is about to read — the token
    that can reach every repository it is scoped to, sitting in a file.

    So the remote is the plain URL and the credential rides on a
    per-invocation `-c http.extraHeader`, which is not persisted anywhere.
    """
    monkeypatch.setattr("shared.config.settings.github_pat", "ghp_supersecret")
    checkout = repo_checkout(TEAM_A, "acme/app")
    checkout.parent.mkdir(parents=True, exist_ok=True)
    _run_git(["clone", "--depth", "1", str(origin), str(checkout)], None, "ghp_supersecret")

    config = (checkout / ".git" / "config").read_text()
    assert "ghp_supersecret" not in config
    for path in (checkout / ".git").rglob("*"):
        if path.is_file():
            try:
                assert "ghp_supersecret" not in path.read_text(errors="ignore")
            except (OSError, UnicodeDecodeError):
                pass


def test_the_token_never_reaches_an_error_message(workspaces, monkeypatch):
    """git echoes the failing command, and the header is in argv. Without
    redaction the credential lands in a log line, a jobs row, and — since a
    handler's exception text becomes the job's failure reason — somewhere a
    human reads."""
    monkeypatch.setattr("shared.config.settings.github_pat", "ghp_supersecret")
    with pytest.raises(RepoSyncError) as exc:
        _run_git(["clone", "--depth", "1", "/nonexistent/repo", "/tmp/nope"],
                 None, "ghp_supersecret")
    assert "ghp_supersecret" not in str(exc.value)


def test_no_credential_configured_is_permanent_not_retryable(workspaces, monkeypatch):
    """Three attempts at a missing credential is three wasted attempts."""
    monkeypatch.setattr("shared.config.settings.github_pat", "")
    with pytest.raises(PermanentJobError, match="no GitHub credential"):
        sync_repo(TEAM_A, "acme/app")


def test_the_auth_header_is_basic_x_access_token(monkeypatch):
    """The scheme GitHub expects for both a PAT and an App installation token,
    which is what lets the App replace _token_for and nothing else."""
    header = _auth_header("ghp_x")
    assert header.startswith("http.extraHeader=Authorization: Basic ")
    from base64 import b64decode
    encoded = header.split("Basic ", 1)[1]
    assert b64decode(encoded).decode() == "x-access-token:ghp_x"


# ---------------------------------------------------------------------------
# Clone and refresh
# ---------------------------------------------------------------------------

def test_a_clone_lands_inside_the_teams_workspace(workspaces, origin, monkeypatch, admin):
    checkout = _sync_local(origin, monkeypatch)
    assert (checkout / "app.py").read_text() == "print('hello')\n"
    assert checkout.parent.name == TEAM_A


def test_a_refresh_discards_what_the_last_turn_left_behind(workspaces, origin, monkeypatch):
    """`reset --hard` and `clean -fd`, not `pull`.

    The agent's edits live in this tree. A turn must start from what the remote
    says rather than from a half-finished change some earlier turn abandoned —
    anything worth keeping has been pushed by then, which is what Phase C's PR
    action is for.
    """
    checkout = _sync_local(origin, monkeypatch)
    (checkout / "app.py").write_text("agent scribbled here\n")
    (checkout / "junk.txt").write_text("left behind\n")

    _sync_local(origin, monkeypatch)
    assert (checkout / "app.py").read_text() == "print('hello')\n"
    assert not (checkout / "junk.txt").exists()


def test_a_second_repo_for_the_same_team_gets_its_own_directory(
    workspaces, origin, monkeypatch
):
    """github_repos is unique on (team_id, repo_full_name), so a team may
    connect several — the checkout is a level below the workspace for exactly
    that reason."""
    a = _sync_local(origin, monkeypatch, "acme/app")
    b = _sync_local(origin, monkeypatch, "acme/other")
    assert a != b
    assert a.parent == b.parent
    assert (a / "app.py").exists() and (b / "app.py").exists()


def test_the_clone_is_recorded_so_it_survives_a_restart(seeded, workspaces, origin,
                                                        monkeypatch, admin):
    """Answered from a column, not by stat-ing a directory: a filesystem check
    is a second source of truth that drifts the moment a disk is wiped or the
    workspaces root is repointed."""
    admin.execute(
        "insert into public.github_repos (team_id, repo_full_name)"
        " values (%s,'acme/app') on conflict do nothing",
        (TEAM_A,),
    )
    _sync_local(origin, monkeypatch, "acme/app")
    assert admin.execute(
        "select last_cloned_at from public.github_repos"
        " where team_id=%s and repo_full_name='acme/app'",
        (TEAM_A,),
    ).fetchone()[0] is not None


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------

def test_syncing_is_queued_not_run_inline(seeded, admin):
    """A clone is slow and fallible, which is what pipeline/worker.py's leases,
    three attempts and PermanentJobError split already handle. A second
    scheduler would be a second thing to get right."""
    job_id = enqueue_sync(TEAM_A, "acme/app")
    row = admin.execute(
        "select job_type, payload, status from public.jobs where id=%s", (job_id,)
    ).fetchone()
    assert row[0] == "sync_repo"
    assert row[1]["repo_full_name"] == "acme/app"
    assert row[2] == "pending"


def test_queueing_twice_is_one_job(seeded, admin):
    first = enqueue_sync(TEAM_A, "acme/app")
    second = enqueue_sync(TEAM_A, "acme/app")
    assert first == second


def test_a_job_with_no_repo_name_is_permanent(seeded):
    with pytest.raises(PermanentJobError, match="no repo_full_name"):
        handle_sync_repo(TEAM_A, {})


def test_the_handler_is_registered():
    """A handler nobody registered is a function the worker never calls."""
    from pipeline.worker import _HANDLERS

    assert "sync_repo" in _HANDLERS


def test_git_cannot_be_given_a_shell_string():
    """argv, never a shell. repo_full_name reaches this from a column that a
    connect-repo screen will one day let a member fill in."""
    import inspect

    source = inspect.getsource(_run_git)
    assert "shell=True" not in source
    assert "timeout=GIT_TIMEOUT_SECONDS" in source
    assert GIT_TIMEOUT_SECONDS < 30 * 60, "must finish inside the worker's lease"


# ---------------------------------------------------------------------------

def _sync_local(origin: Path, monkeypatch, repo_full_name: str = "acme/app") -> Path:
    """sync_repo against a local origin — real git, no network, no token."""
    monkeypatch.setattr("shared.config.settings.github_pat", "unused-locally")
    monkeypatch.setattr("pipeline.repo_sync._url_for", lambda _name: str(origin))
    return sync_repo(TEAM_A, repo_full_name)


def test_the_pipeline_cannot_repoint_a_repository(seeded, admin):
    """Column-level grant, and this is what it buys.

    pl_github_repos is `for all`, so the POLICY would let comrade_pipeline
    rewrite any column. The grant is `update (last_cloned_at)` alone, because
    repo_full_name decides which checkout directory exists and which remote is
    cloned — a pipeline that could rewrite it could point a team's workspace at
    somebody else's repository. That stays a member action under
    au_github_repos_update.
    """
    import psycopg as _psycopg

    from shared.db import Role, team_session

    admin.execute(
        "insert into public.github_repos (team_id, repo_full_name)"
        " values (%s,'acme/app') on conflict do nothing",
        (TEAM_A,),
    )
    with pytest.raises(_psycopg.errors.InsufficientPrivilege):
        with team_session(Role.PIPELINE, TEAM_A) as conn:
            conn.execute(
                "update public.github_repos set repo_full_name='attacker/evil'"
                " where team_id=%s",
                (TEAM_A,),
            )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def _age(path: Path, seconds: int) -> None:
    """Backdate a directory past the sweep's grace window."""
    import os

    old = time.time() - seconds
    os.utime(path, (old, old))


def test_a_deleted_teams_checkout_is_reclaimed(seeded, workspaces, monkeypatch):
    """The reconciler's job. A team can be deleted while the worker is down,
    so this converges rather than relying on having heard an event."""
    from pipeline.repo_sync import sweep_orphan_workspaces
    from shared.workspace import ensure_workspace

    gone = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    stale = ensure_workspace(gone)
    (stale / "app.py").write_text("x")
    _age(stale, SWEEP_GRACE_SECONDS + 60)

    assert gone in sweep_orphan_workspaces()
    assert not stale.exists()


def test_a_live_teams_checkout_survives_the_sweep(seeded, workspaces):
    from pipeline.repo_sync import sweep_orphan_workspaces
    from shared.workspace import ensure_workspace

    live = ensure_workspace(TEAM_A)
    (live / "app.py").write_text("x")
    _age(live, SWEEP_GRACE_SECONDS + 60)

    sweep_orphan_workspaces()
    assert (live / "app.py").exists()


def test_a_freshly_made_workspace_is_never_swept(seeded, workspaces):
    """The race the grace window exists for.

    The sweep crosses teams on its own connection, so a checkout created
    moments ago can be invisible to its snapshot. Without the window the
    reconciler deletes a workspace out from under the turn that just made it.
    """
    from pipeline.repo_sync import sweep_orphan_workspaces
    from shared.workspace import ensure_workspace

    brand_new = ensure_workspace("dddddddd-dddd-dddd-dddd-dddddddddddd")
    assert sweep_orphan_workspaces() == []
    assert brand_new.exists()


def test_the_sweep_ignores_anything_that_is_not_a_uuid(seeded, workspaces):
    """A reconciler that deletes what it does not recognise is how one eats a
    directory somebody else was using."""
    from pipeline.repo_sync import sweep_orphan_workspaces
    from shared.workspace import workspaces_root

    root = workspaces_root()
    root.mkdir(parents=True, exist_ok=True)
    stranger = root / "not-ours"
    stranger.mkdir()
    (stranger / "important.txt").write_text("someone else's")
    _age(stranger, SWEEP_GRACE_SECONDS + 60)

    sweep_orphan_workspaces()
    assert (stranger / "important.txt").exists()


def test_the_disk_cap_evicts_oldest_first(seeded, workspaces, monkeypatch):
    """A full disk takes Postgres with it. Eviction is safe here in a way it
    is not for most caches: a checkout is a copy of something GitHub still
    has, and the next turn re-clones it."""
    from pipeline.repo_sync import enforce_disk_cap
    from shared.workspace import ensure_workspace

    monkeypatch.setattr(
        "shared.config.settings.comrade_workspaces_max_gb", 1.0 / 1024 / 1024
    )
    old = ensure_workspace("11111111-1111-1111-1111-111111111111")
    new = ensure_workspace("22222222-2222-2222-2222-222222222222")
    (old / "big.bin").write_bytes(b"x" * 4096)
    (new / "big.bin").write_bytes(b"x" * 4096)
    _age(old, 10_000)

    evicted = enforce_disk_cap()
    assert "11111111-1111-1111-1111-111111111111" in evicted
    assert not old.exists()


def test_under_budget_nothing_is_evicted(seeded, workspaces):
    from pipeline.repo_sync import enforce_disk_cap
    from shared.workspace import ensure_workspace

    keep = ensure_workspace(TEAM_A)
    (keep / "small.txt").write_text("x")
    assert enforce_disk_cap() == []
    assert keep.exists()


def test_a_repo_cloned_while_empty_recovers_once_it_has_commits(
    workspaces, tmp_path, monkeypatch
):
    """🔴 Found on the first real run against github.com, and permanent.

    A team connects a repository they just created on GitHub. It has no
    commits, so `git clone` succeeds with a warning and writes NO
    refs/remotes/origin/HEAD. They push their first commit an hour later.

    Every turn from then on failed. `origin/HEAD` is written by `git clone`
    and NEVER by `git fetch`, so the checkout could not repair itself — the
    only fix was deleting the workspace by hand, and nothing in the product
    told anyone that. "unknown revision origin/HEAD" is also not a sentence
    that suggests it.

    Asking the remote for its default branch needs no local ref, so the same
    checkout heals on the next sync.
    """
    monkeypatch.setattr("shared.config.settings.github_pat", "unused-locally")
    empty = tmp_path / "late.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(empty)],
                   check=True, capture_output=True)
    monkeypatch.setattr("pipeline.repo_sync._url_for", lambda _n: str(empty))

    sync_repo(TEAM_A, "acme/late")           # cloned while empty

    seed = tmp_path / "seed"
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", *a], cwd=seed, check=True, capture_output=True
    )
    subprocess.run(["git", "clone", "-q", str(empty), str(seed)],
                   check=True, capture_output=True)
    run("config", "user.email", "t@test.dev")
    run("config", "user.name", "Test")
    (seed / "app.py").write_text("print('late')\n")
    run("add", "-A")
    run("commit", "-q", "-m", "first commit, an hour later")
    run("push", "-q", "origin", "main")

    checkout = sync_repo(TEAM_A, "acme/late")
    assert (checkout / "app.py").read_text() == "print('late')\n"


def test_an_empty_repo_is_named_as_empty_not_as_a_missing_ref(
    workspaces, tmp_path, monkeypatch
):
    """The message a human has to act on. "no commits yet" tells them what to
    do; "unknown revision origin/HEAD" tells them to open a bug."""
    monkeypatch.setattr("shared.config.settings.github_pat", "unused-locally")
    empty = tmp_path / "empty.git"
    subprocess.run(["git", "init", "-q", "--bare", str(empty)],
                   check=True, capture_output=True)
    monkeypatch.setattr("pipeline.repo_sync._url_for", lambda _n: str(empty))
    sync_repo(TEAM_A, "acme/empty")

    from pipeline.repo_sync import default_branch
    with pytest.raises(RepoSyncError, match="no commits yet"):
        default_branch(TEAM_A, "acme/empty")


def test_disconnecting_a_repo_reclaims_its_checkout(
    seeded, workspaces, origin, monkeypatch, admin
):
    """🔴 Nothing server-side hears about a disconnect.

    Removing a repository is a row delete the frontend makes straight to
    Supabase under RLS. The checkout would otherwise sit on disk indefinitely
    holding a copy of code the team has told us to stop reading — which is the
    part that matters, rather than the bytes.

    Reclaimed by the SWEEP rather than by a delete hook, for the same reason
    the team-level orphan case is: a reconciler converges whether the row went
    away through the UI, through psql, or while the worker was down.
    """
    monkeypatch.setattr("shared.config.settings.github_pat", "unused-locally")
    monkeypatch.setattr("pipeline.repo_sync._url_for", lambda _n: str(origin))
    admin.execute("delete from public.github_repos where team_id=%s", (TEAM_A,))
    admin.execute(
        "insert into public.github_repos (team_id, repo_full_name) values (%s,%s)",
        (TEAM_A, "acme/app"),
    )
    checkout = sync_repo(TEAM_A, "acme/app")
    assert (checkout / "app.py").exists()

    # Age it past the grace window: a checkout being cloned right now has no
    # row visible to the snapshot the sweep takes, and deleting it would race
    # the clone that is still writing it.
    old = time.time() - SWEEP_GRACE_SECONDS - 60
    os.utime(checkout, (old, old))

    # Still connected -> untouched, so the test below is about the row and not
    # about the grace window.
    sweep_orphan_workspaces()
    assert checkout.exists()

    admin.execute(
        "delete from public.github_repos where team_id=%s and repo_full_name=%s",
        (TEAM_A, "acme/app"),
    )
    sweep_orphan_workspaces()
    assert not checkout.exists()
    # The team's workspace itself survives: the team is still live, and only
    # the repository they disconnected went.
    assert checkout.parent.exists()


def test_connecting_a_repo_eventually_makes_it_visible_to_the_agent(
    seeded, workspaces, origin, monkeypatch, admin
):
    """🔴 The gap that made every repository tool unreachable through the product.

    `enqueue_sync` was written, tested, and never called from anywhere. So
    connecting a repository inserted the row and queued nothing; last_cloned_at
    stayed null; and `connected_repo` filters on exactly that column — so the
    agent answered "no repository is connected" to a team looking at their
    repository listed as connected on the setup screen. Phases B, C and D were
    all complete and all unreachable.

    This walks the whole path a real connection takes, which is the only shape
    of test that would have noticed: the unit tests for enqueue_sync and for
    sync_repo both passed the entire time.
    """
    from agent.repo_tools import connected_repo

    monkeypatch.setattr("shared.config.settings.github_pat", "unused-locally")
    monkeypatch.setattr("pipeline.repo_sync._url_for", lambda _n: str(origin))
    admin.execute("delete from public.github_repos where team_id=%s", (TEAM_A,))
    admin.execute(
        "insert into public.github_repos (team_id, repo_full_name) values (%s,%s)",
        (TEAM_A, "acme/app"),
    )

    # The symptom, before anything runs: connected, and invisible.
    assert connected_repo(TEAM_A, A1) is None

    # THROUGH tick(), not by calling the sweep directly. The bug was that
    # sweep_stale_checkouts' predecessor was never CALLED — enqueue_sync had
    # its own passing unit test the whole time. A test that reaches past the
    # worker to the function it is testing would have gone green on the broken
    # code, which is the entire reason this one exists.
    from pipeline.worker import run_once, tick

    tick()
    # BOUNDED. run_once returns True for a job it re-queued after a transient
    # failure, so `while run_once(): pass` only terminates if every failure
    # eventually exhausts its attempts — more faith than a loop a test cannot
    # interrupt deserves.
    for _ in range(20):
        if not run_once():
            break

    assert connected_repo(TEAM_A, A1) == "acme/app"
    assert (repo_checkout(TEAM_A, "acme/app") / "app.py").exists()


def test_a_fresh_checkout_is_not_re_cloned_every_tick(
    seeded, workspaces, origin, monkeypatch, admin
):
    """The reconciler must converge, not thrash. A sweep that re-queued every
    connected repository on every tick would clone continuously and keep the
    queue permanently busy."""
    monkeypatch.setattr("shared.config.settings.github_pat", "unused-locally")
    monkeypatch.setattr("pipeline.repo_sync._url_for", lambda _n: str(origin))
    admin.execute("delete from public.github_repos where team_id=%s", (TEAM_A,))
    admin.execute(
        "insert into public.github_repos (team_id, repo_full_name, last_cloned_at)"
        " values (%s,%s, now())",
        (TEAM_A, "acme/app"),
    )
    assert sweep_stale_checkouts() == []


def test_a_failing_clone_is_not_retried_at_full_speed(
    seeded, workspaces, monkeypatch, admin
):
    """🔴 A reconciler that retries a permanent failure as fast as the machine
    allows converges on nothing but heat.

    tick() drains the queue and THEN sweeps. A failed sync is no longer
    'pending', so enqueue_sync's dedupe stops applying and the sweep re-queues
    it at once — and because tick() processed something, the worker skips its
    sleep and goes straight round again. Four failed attempts inside one second
    were observed from a single misconfigured repository, which is a CPU pinned
    and a jobs table filling for as long as nobody notices.
    """
    from psycopg.types.json import Json

    admin.execute("delete from public.github_repos where team_id=%s", (TEAM_A,))
    admin.execute("delete from public.jobs where team_id=%s", (TEAM_A,))
    admin.execute(
        "insert into public.github_repos (team_id, repo_full_name) values (%s,%s)",
        (TEAM_A, "acme/broken"),
    )
    # It would be queued right now, with nothing having failed yet.
    assert sweep_stale_checkouts()

    admin.execute("delete from public.jobs where team_id=%s", (TEAM_A,))
    admin.execute(
        "insert into public.jobs"
        " (team_id, job_type, payload, status, last_error, finished_at)"
        " values (%s,'sync_repo',%s,'failed','no credential', now())",
        (TEAM_A, Json({"repo_full_name": "acme/broken"})),
    )
    assert sweep_stale_checkouts() == [], "a just-failed clone was retried immediately"

    # And it IS retried once the backoff has passed — a reconciler that gives
    # up permanently is not a reconciler.
    admin.execute(
        "update public.jobs set finished_at = now() - make_interval(secs => %s)"
        " where team_id=%s",
        (SYNC_RETRY_BACKOFF_SECONDS + 60, TEAM_A),
    )
    assert sweep_stale_checkouts(), "the clone was never retried after the backoff"
    admin.execute("delete from public.jobs where team_id=%s", (TEAM_A,))
