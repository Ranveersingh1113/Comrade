"""An environment a team asked for, whose state they can see.

The feature exists so the agent can tell a member "your tests failed" apart
from "I had nothing to run them in". Most of what is worth testing is therefore
about the STATUS being honest — including when the environment is taken away.
"""
import psycopg
import pytest

from pipeline.repo_deps import environment_key
from pipeline.repo_env import (
    _bytes_from, enqueue_build, handle_build_environment, status_for,
    sweep_environments,
)
from shared.config import settings
from shared.db import user_session
from tests._seed import A1, A2, TEAM_A

REPO = "acme/app"


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
def connected(admin, tmp_path, monkeypatch):
    """A repository connected, cloned, with a manifest on disk."""
    monkeypatch.setattr(
        "shared.config.settings.comrade_workspaces_root", str(tmp_path / "ws")
    )
    from shared.workspace import repo_checkout

    root = repo_checkout(TEAM_A, REPO)
    root.mkdir(parents=True)
    (root / "requirements.txt").write_text("cowsay==6.1\n")
    admin.execute(
        "insert into public.github_repos (team_id, repo_full_name, last_cloned_at)"
        " values (%s,%s, now())",
        (TEAM_A, REPO),
    )
    return root


def _set(admin, **cols):
    sets = ", ".join(f"{k} = %s" for k in cols)
    admin.execute(
        f"update public.github_repos set {sets}"
        " where team_id = %s and repo_full_name = %s",
        (*cols.values(), TEAM_A, REPO),
    )


# ---------------------------------------------------------------------------
# The status a member is shown
# ---------------------------------------------------------------------------

def test_disabled_is_the_default_and_says_what_that_means(seeded, connected, admin):
    """🔴 Connecting a repository is not permission to run its build hooks.

    So `false` is the default, and the message tells a member what they are
    missing and who can change it rather than leaving them to infer it from an
    ImportError.
    """
    out = status_for(TEAM_A, REPO, A1)
    assert out["status"] == "disabled"
    assert "team lead" in out["detail"]


def test_enabled_but_unbuilt_is_not_the_same_as_failed(seeded, connected, admin):
    _set(admin, env_enabled=True)
    assert status_for(TEAM_A, REPO, A1)["status"] == "none"


def test_a_failed_build_repeats_the_reason(seeded, connected, admin):
    """The reason is pip's, and the team is the only party who can act on it —
    so it is carried through rather than replaced with 'something went wrong'."""
    _set(admin, env_enabled=True, env_status="failed",
         env_error="No matching distribution found for nope==1.0")
    out = status_for(TEAM_A, REPO, A1)
    assert out["status"] == "failed"
    assert "No matching distribution" in out["detail"]


def test_ready_means_built_from_what_the_checkout_says_now(seeded, connected, admin):
    _set(admin, env_enabled=True, env_status="ready",
         env_key=environment_key(connected, "requirements.txt"))
    assert status_for(TEAM_A, REPO, A1)["status"] == "ready"


def test_a_changed_manifest_makes_a_ready_environment_stale(
    seeded, connected, admin
):
    """🔴 Stale is DERIVED, never stored.

    Storing it would be a second copy of something already known — the key and
    the checkout — and free to drift from both. The comparison cannot go stale
    the way a flag can.
    """
    _set(admin, env_enabled=True, env_status="ready",
         env_key=environment_key(connected, "requirements.txt"))
    assert status_for(TEAM_A, REPO, A1)["status"] == "ready"

    (connected / "requirements.txt").write_text("cowsay==6.1\nsix==1.17.0\n")
    out = status_for(TEAM_A, REPO, A1)
    assert out["status"] == "stale"
    assert "out of date" in out["detail"]


# ---------------------------------------------------------------------------
# Who may turn it on
# ---------------------------------------------------------------------------

def test_the_pipeline_cannot_grant_the_permission_it_reports_on(seeded, connected):
    """🔴 The column grant, checked.

    comrade_pipeline holds UPDATE on the status columns and NOT on env_enabled.
    A worker that could set env_enabled could turn on the very capability a
    member declined — the distance between "reports what happened" and "decides
    what may happen" is exactly what a column grant expresses.
    """
    from shared.db import Role, team_session

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with team_session(Role.PIPELINE, TEAM_A) as conn:
            conn.execute(
                "update public.github_repos set env_enabled = true"
                " where team_id = %s", (TEAM_A,)
            )

    # And it CAN report status, so the refusal above is about the column.
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        conn.execute(
            "update public.github_repos set env_status = 'building'"
            " where team_id = %s", (TEAM_A,)
        )


def test_a_plain_member_cannot_turn_it_on(seeded, connected):
    """Enabling rides on au_github_repos_update, which requires leadership —
    the same policy that governs every other change to this row, rather than a
    second one that could be wrong differently."""
    # 🔴 RLS REFUSES BY FILTERING, NOT BY ERRORING. An UPDATE whose USING
    # clause does not match touches ZERO ROWS and raises nothing — so a route
    # that ran this and returned "ok" would tell a member their change was
    # saved when the database ignored it. The assertion is on the rowcount and
    # on the value, because an exception never comes.
    with user_session(A2) as conn:
        result = conn.execute(
            "update public.github_repos set env_enabled = true"
            " where team_id = %s and repo_full_name = %s",
            (TEAM_A, REPO),
        )
        assert result.rowcount == 0, "a plain member changed env_enabled"

    with user_session(A1) as conn:
        still = conn.execute(
            "select env_enabled from public.github_repos"
            " where team_id = %s and repo_full_name = %s",
            (TEAM_A, REPO),
        ).fetchone()
    assert still[0] is False


# ---------------------------------------------------------------------------
# The reconciler builds only what was asked for
# ---------------------------------------------------------------------------

def test_the_sweep_ignores_a_repository_nobody_enabled(seeded, connected, admin):
    """🔴 The correction that produced this whole design, guarded.

    An earlier version installed a manifest the moment a repository was
    connected. The sweep must fire only where a leader said so.
    """
    assert sweep_environments() == []
    admin.execute("delete from public.jobs where team_id = %s", (TEAM_A,))

    _set(admin, env_enabled=True)
    queued = sweep_environments()
    assert any(REPO in q for q in queued), queued


def test_a_ready_and_current_environment_is_not_rebuilt(seeded, connected, admin):
    """A sweep runs every few seconds. Rebuilding a venv each time would make
    the worker useless for anything else."""
    _set(admin, env_enabled=True, env_status="ready",
         env_key=environment_key(connected, "requirements.txt"))
    assert sweep_environments() == []

    (connected / "requirements.txt").write_text("cowsay==6.2\n")
    assert any(REPO in q for q in sweep_environments())


def test_turning_it_off_between_queue_and_build_is_obeyed(seeded, connected, admin):
    """🔴 The answer to "should this be built" is read at BUILD time.

    A member who changes their mind while a job sits in the queue is obeyed
    rather than raced — otherwise revoking the permission would still run the
    install it was revoked to prevent.
    """
    _set(admin, env_enabled=True)
    enqueue_build(TEAM_A, REPO)
    _set(admin, env_enabled=False)

    handle_build_environment(TEAM_A, {"repo_full_name": REPO})
    row = admin.execute(
        "select env_status from public.github_repos where team_id = %s",
        (TEAM_A,),
    ).fetchone()
    assert row[0] is None, "a disabled repository was built anyway"


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

    _set(admin, env_enabled=True, env_status="ready",
         env_key=environment_key(connected, "requirements.txt"))

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

    _set(admin, env_enabled=True, env_status="ready", env_key="k")
    monkeypatch.setattr(
        repo_env, "_environment_volumes",
        lambda: {deps_volume(TEAM_A, REPO): 1024**2},
    )
    monkeypatch.setattr("shared.config.settings.comrade_env_max_gb", 10.0)
    assert repo_env.enforce_env_disk_cap() == []
