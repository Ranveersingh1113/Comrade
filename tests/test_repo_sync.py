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
import subprocess
from pathlib import Path

import psycopg
import pytest

from pipeline.repo_sync import (
    GIT_TIMEOUT_SECONDS, RepoSyncError, _auth_header, _run_git, enqueue_sync,
    handle_sync_repo, sync_repo,
)
from pipeline.worker import PermanentJobError
from shared.config import settings
from shared.workspace import repo_checkout
from tests._seed import TEAM_A


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
