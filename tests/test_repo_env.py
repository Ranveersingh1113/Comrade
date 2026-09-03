"""An environment a team asked for, whose state they can see.

Installing a repository's dependencies runs its build hooks with network
access, so it is never implied by connecting a repository. The tests that
matter here are about the OPT-IN and about the STATE being honest — an
environment that silently is not there turns every red test suite into a
mystery, and a pass from a stale one is worse than a failure.
"""
import subprocess

import psycopg
import pytest

from pipeline.repo_deps import environment_key
from pipeline.repo_env import (
    _bytes_from, enqueue_build, handle_build_environment, status_for,
    sweep_environments,
)
from shared.config import settings
from shared.workspace import deps_volume, repo_checkout
from tests._seed import A1, TEAM_A

REPO = "acme/env"


def _docker_up() -> bool:
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=30
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


needs_docker = pytest.mark.skipif(not _docker_up(), reason="Docker is not running")


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.execute("delete from public.jobs where team_id = %s", (TEAM_A,))
        conn.execute("delete from public.github_repos where team_id = %s", (TEAM_A,))
        conn.close()


@pytest.fixture
def connected(tmp_path, monkeypatch, admin):
    """A cloned repository with a manifest, connected but NOT enabled."""
    monkeypatch.setattr(
        "shared.config.settings.comrade_workspaces_root", str(tmp_path / "ws")
    )
    admin.execute(
        "insert into public.github_repos (team_id, repo_full_name, last_cloned_at)"
        " values (%s,%s, now())",
        (TEAM_A, REPO),
    )
    root = repo_checkout(TEAM_A, REPO)
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    (root / "requirements.txt").write_text("cowsay==6.1\n")
    return root


def _enable(admin, value=True):
    admin.execute(
        "update public.github_repos set env_enabled = %s where team_id = %s",
        (value, TEAM_A),
    )


# ---------------------------------------------------------------------------
# The opt-in
# ---------------------------------------------------------------------------

def test_nothing_is_built_for_a_repository_nobody_enabled(seeded, connected, admin):
    """🔴 The correction this whole module exists for.

    Dependency installation was briefly a side effect of connecting a
    repository. Connecting is a READ consent — "Comrade can see our code" —
    and installing a manifest unattended turns it into "Comrade may execute
    this repository's dependency graph, with egress".
    """
    assert status_for(TEAM_A, REPO, A1)["status"] == "disabled"
    assert sweep_environments() == []


def test_enabling_is_what_queues_a_build(seeded, connected, admin):
    assert status_for(TEAM_A, REPO, A1)["status"] == "disabled"
    _enable(admin)
    assert status_for(TEAM_A, REPO, A1)["status"] == "none"
    assert sweep_environments(), "enabling queued nothing"


def test_the_pipeline_cannot_turn_the_environment_on(seeded, connected):
    """🔴 A worker that could set env_enabled could grant itself the very
    capability a member declined.

    The pipeline holds a COLUMN grant on the status fields and none on
    env_enabled — it reports what the environment is doing and never decides
    whether the team wanted one.
    """
    from shared.db import Role, team_session

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with team_session(Role.PIPELINE, TEAM_A) as conn:
            conn.execute(
                "update public.github_repos set env_enabled = true"
                " where team_id = %s", (TEAM_A,),
            )


def test_turning_it_off_between_queueing_and_running_is_obeyed(
    seeded, connected, admin
):
    """🔴 The answer to "should this be built" is read at BUILD time.

    A member who changes their mind after a job is queued must be obeyed
    rather than raced — otherwise the one action they took to stop it is the
    one the system ignores.
    """
    _enable(admin)
    enqueue_build(TEAM_A, REPO)
    _enable(admin, False)

    handle_build_environment(TEAM_A, {"repo_full_name": REPO})

    row = admin.execute(
        "select env_status from public.github_repos where team_id = %s", (TEAM_A,)
    ).fetchone()
    assert row[0] is None, "it built for a repository that had been turned off"


# ---------------------------------------------------------------------------
# The state is honest
# ---------------------------------------------------------------------------

def test_a_failed_build_says_why(seeded, connected, admin, monkeypatch):
    """A resolution conflict is a fact about the project, and the team is the
    only party who can act on it. Recording 'failed' without the reason makes
    that impossible."""
    _enable(admin)
    monkeypatch.setattr(
        "pipeline.repo_env.install",
        lambda *_: {"status": "failed",
                    "detail": "No matching distribution found for nope==9"},
    )
    handle_build_environment(TEAM_A, {"repo_full_name": REPO})

    state = status_for(TEAM_A, REPO, A1)
    assert state["status"] == "failed"
    assert "No matching distribution" in state["detail"]


def test_a_changed_manifest_makes_it_stale(seeded, connected, admin, monkeypatch):
    _enable(admin)
    monkeypatch.setattr("pipeline.repo_env.install",
                        lambda *_: {"status": "installed"})
    handle_build_environment(TEAM_A, {"repo_full_name": REPO})
    assert status_for(TEAM_A, REPO, A1)["status"] == "ready"

    (connected / "requirements.txt").write_text("cowsay==6.1\nsix==1.17.0\n")
    assert status_for(TEAM_A, REPO, A1)["status"] == "stale"


def test_a_stale_environment_is_rebuilt_but_a_current_one_is_not(
    seeded, connected, admin, monkeypatch
):
    """The reconciler must converge, not thrash: it runs on every tick, and
    re-queueing a current environment would rebuild forever."""
    _enable(admin)
    monkeypatch.setattr("pipeline.repo_env.install",
                        lambda *_: {"status": "installed"})
    handle_build_environment(TEAM_A, {"repo_full_name": REPO})

    assert sweep_environments() == [], "a current environment was re-queued"

    (connected / "requirements.txt").write_text("cowsay==6.2\n")
    assert sweep_environments(), "a stale environment was not re-queued"


# ---------------------------------------------------------------------------
# The cache key
# ---------------------------------------------------------------------------

def test_the_commit_is_in_the_key_only_when_the_repo_installs_itself(
    seeded, connected
):
    """🔴 The asymmetry is the point.

    `pip install /workspace` installs the repository's OWN package, so a commit
    that changes source without touching pyproject.toml would otherwise leave a
    stale build installed and the tests running against code that is not in the
    checkout.

    The requirements.txt path installs only third-party packages, so folding
    the commit in would rebuild on every push without changing a byte — and an
    environment that rebuilds for three minutes per commit is one a team turns
    off.
    """
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", *a], cwd=connected, check=True, capture_output=True
    )
    (connected / "pyproject.toml").write_text("[project]\nname='x'\nversion='1'\n")
    run("add", "-A")
    run("-c", "user.email=t@t.dev", "-c", "user.name=T", "commit", "-qm", "one")

    req_before = environment_key(connected, "requirements.txt")
    proj_before = environment_key(connected, "pyproject.toml")

    (connected / "src.py").write_text("print('changed source')\n")
    run("add", "-A")
    run("-c", "user.email=t@t.dev", "-c", "user.name=T", "commit", "-qm", "two")

    assert environment_key(connected, "requirements.txt") == req_before, (
        "a source-only commit rebuilt an environment of third-party packages"
    )
    assert environment_key(connected, "pyproject.toml") != proj_before, (
        "a source-only commit left the repository's own package stale"
    )


def test_a_lockfile_is_part_of_the_key(seeded, connected):
    """A lockfile pins the RESOLVED set, so it changing means the installed
    packages change even when the manifest naming them did not."""
    before = environment_key(connected, "requirements.txt")
    (connected / "uv.lock").write_text("version = 1\n")
    after = environment_key(connected, "requirements.txt")
    assert after != before
    (connected / "uv.lock").write_text("version = 2\n")
    assert environment_key(connected, "requirements.txt") != after


def test_changing_the_recipe_invalidates_every_environment(
    seeded, connected, monkeypatch
):
    """🔴 Without a recipe version, a volume built by an older install script
    keeps a key that still matches and is never rebuilt — it silently keeps
    whatever layout the old script produced."""
    before = environment_key(connected, "requirements.txt")
    monkeypatch.setattr("pipeline.repo_deps.RECIPE_VERSION", "2")
    assert environment_key(connected, "requirements.txt") != before


# ---------------------------------------------------------------------------
# What the agent is told
# ---------------------------------------------------------------------------

@needs_docker
def test_repo_run_reports_the_environment_on_every_result(
    seeded, connected, admin, monkeypatch
):
    """🔴 The distinction this feature exists for.

    Without it the agent cannot tell "your tests failed" from "I had nothing to
    run them in". An import error with no environment is not evidence about
    anyone's code — and a PASS from a stale environment is the more dangerous
    report, because it is the one somebody acts on.
    """
    from agent.repo_tools import repo_run

    class Ctx:
        state = {"team_id": TEAM_A, "requester_id": A1, "repo_full_name": REPO}

    (connected / "t.py").write_text("import cowsay\n")
    result = repo_run("python t.py", Ctx())
    assert result["exit_code"] != 0          # no environment, so it cannot import
    assert result["environment"]["status"] == "disabled"
    assert "turn one on" in result["environment"]["detail"]

    # And on a SUCCESS too.
    (connected / "t.py").write_text("print('fine')\n")
    ok = repo_run("python t.py", Ctx())
    assert ok["exit_code"] == 0
    assert ok["environment"]["status"] == "disabled"



# ---------------------------------------------------------------------------
# Disk: these volumes share a disk with Postgres
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("1.234GB", 1_234_000_000), ("500MB", 500_000_000),
    ("3.282kB", 3_282), ("0B", 0),
    # 🔴 Zero on failure, and the DIRECTION is the point: an unreadable size
    # makes the total look smaller, so nothing is evicted. Guessing high would
    # delete a team's environment because a format changed.
    ("garbage", 0), ("", 0), (None, 0),
])
def test_docker_sizes_parse_and_fail_safe(text, expected):
    assert _bytes_from(text) == expected


def test_eviction_clears_the_status_it_invalidates(seeded, connected, admin,
                                                   monkeypatch):
    """🔴 A volume removed while the row still says 'ready' is the database
    asserting something untrue.

    The agent would mount an environment that no longer exists and the member
    would be told their tests failed rather than that their environment was
    reclaimed. Every silent-success bug on this branch had this shape.
    """
    from pipeline import repo_env
    from shared.workspace import deps_volume

    _enable(admin)
    admin.execute(
        "update public.github_repos set env_status='ready', env_key=%s"
        " where team_id=%s and repo_full_name=%s",
        (environment_key(connected, "requirements.txt"), TEAM_A, REPO),
    )

    volume = deps_volume(TEAM_A, REPO)
    monkeypatch.setattr(repo_env, "_environment_volumes",
                        lambda: {volume: 50 * 1024**3})
    monkeypatch.setattr("shared.config.settings.comrade_env_max_gb", 1.0)
    monkeypatch.setattr("pipeline.repo_env.subprocess.run",
                        lambda *a, **k: type("P", (), {"returncode": 0})())

    evicted = repo_env.enforce_env_disk_cap()
    assert any(REPO in e for e in evicted), evicted

    row = admin.execute(
        "select env_status, env_error from public.github_repos"
        " where team_id = %s", (TEAM_A,)
    ).fetchone()
    assert row[0] is None, "the row still claims an environment that was removed"
    assert "reclaimed" in (row[1] or "")


def test_nothing_is_evicted_while_under_budget(seeded, connected, admin,
                                               monkeypatch):
    from pipeline import repo_env
    from shared.workspace import deps_volume

    _enable(admin)
    admin.execute(
        "update public.github_repos set env_status='ready', env_key='k'"
        " where team_id=%s and repo_full_name=%s", (TEAM_A, REPO),
    )
    monkeypatch.setattr(
        repo_env, "_environment_volumes",
        lambda: {deps_volume(TEAM_A, REPO): 1024**2},
    )
    monkeypatch.setattr("shared.config.settings.comrade_env_max_gb", 10.0)
    assert repo_env.enforce_env_disk_cap() == []
