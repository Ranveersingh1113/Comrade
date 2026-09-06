"""Two threads working on one repository must not share a working tree.

Task 15. Until now `workspace_for(team_id)` gave a team ONE checkout, so two
members working at the same time edited the same files: whoever proposed
second proposed both changes, and a `reset --hard` at the start of either turn
threw away the other's work. Concurrency was in the queue and nowhere on disk.

A thread gets a git WORKTREE of the team's checkout: its own files, sharing
one object database, created without a credential because nothing is fetched.
"""
import subprocess
from pathlib import Path

import pytest

from shared.workspace import WorkspaceError, repo_checkout, thread_checkout
from tests._seed import TEAM_A, TEAM_B

REPO = "octo/demo"
THREAD_1 = "11111111-1111-1111-1111-111111111111"
THREAD_2 = "22222222-2222-2222-2222-222222222222"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def origin(tmp_path):
    work = tmp_path / "seed"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@test.dev")
    _git(work, "config", "user.name", "Test")
    (work / "app.py").write_text("print('hello')\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "initial")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)],
                   check=True, capture_output=True)
    return bare


@pytest.fixture
def base(tmp_path, monkeypatch, origin):
    monkeypatch.setattr(
        "shared.config.settings.comrade_workspaces_root", str(tmp_path / "ws")
    )
    monkeypatch.setattr("shared.config.settings.github_pat", "unused-locally")
    monkeypatch.setattr("pipeline.repo_sync._url_for", lambda _n: str(origin))
    from pipeline.repo_sync import sync_repo

    return sync_repo(TEAM_A, REPO)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def test_two_threads_get_different_directories():
    one = thread_checkout(TEAM_A, THREAD_1, REPO)
    two = thread_checkout(TEAM_A, THREAD_2, REPO)
    assert one != two


def test_a_thread_checkout_stays_inside_its_own_team(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "shared.config.settings.comrade_workspaces_root", str(tmp_path / "ws")
    )
    from shared.workspace import workspace_for

    inside = thread_checkout(TEAM_A, THREAD_1, REPO)
    assert workspace_for(TEAM_A) in inside.parents
    assert workspace_for(TEAM_B) not in inside.parents


@pytest.mark.parametrize("bad", ["../escape", "..", "", "not-a-uuid", "/etc"])
def test_a_thread_id_that_is_not_a_uuid_cannot_name_a_directory(bad):
    """Same rule team_id already carries: this string becomes a directory
    name, and a UUID cannot climb, cannot be absolute, and cannot collide."""
    with pytest.raises(WorkspaceError):
        thread_checkout(TEAM_A, bad, REPO)


# ---------------------------------------------------------------------------
# The worktree itself
# ---------------------------------------------------------------------------

def test_each_thread_edits_its_own_copy_of_the_same_file(base):
    from pipeline.repo_sync import ensure_thread_checkout

    one = ensure_thread_checkout(TEAM_A, THREAD_1, REPO)
    two = ensure_thread_checkout(TEAM_A, THREAD_2, REPO)

    (one / "app.py").write_text("print('one')\n")
    (two / "app.py").write_text("print('two')\n")

    assert (one / "app.py").read_text() == "print('one')\n"
    assert (two / "app.py").read_text() == "print('two')\n"
    # and the team's base tree is untouched by either
    assert (base / "app.py").read_text() == "print('hello')\n"


def test_a_thread_keeps_its_work_across_calls(base):
    """A second turn in the same thread continues where the first stopped —
    the point of moving the agent's tree off the shared checkout."""
    from pipeline.repo_sync import ensure_thread_checkout

    first = ensure_thread_checkout(TEAM_A, THREAD_1, REPO)
    (first / "app.py").write_text("print('in progress')\n")
    again = ensure_thread_checkout(TEAM_A, THREAD_1, REPO)

    assert again == first
    assert (again / "app.py").read_text() == "print('in progress')\n"


def test_syncing_the_base_does_not_discard_a_thread_in_flight(base, origin):
    """`sync_repo` resets the base to the remote at the start of a turn. That
    is exactly what used to delete another member's uncommitted work."""
    from pipeline.repo_sync import ensure_thread_checkout, sync_repo

    tree = ensure_thread_checkout(TEAM_A, THREAD_1, REPO)
    (tree / "app.py").write_text("print('mine')\n")
    sync_repo(TEAM_A, REPO)

    assert (tree / "app.py").read_text() == "print('mine')\n"


def test_the_worktree_needs_no_credential(base, monkeypatch):
    """Nothing is fetched to make one, so no token is minted. The mirror is
    where credentials live; a thread's tree is a local operation."""
    from pipeline import repo_sync

    def _refuse(*_a, **_k):
        raise AssertionError("a worktree must not mint a credential")

    monkeypatch.setattr(repo_sync, "_token_for", _refuse, raising=False)
    assert repo_sync.ensure_thread_checkout(TEAM_A, THREAD_1, REPO).exists()


# ---------------------------------------------------------------------------
# capture_patch
# ---------------------------------------------------------------------------

def test_the_patch_comes_from_the_asking_thread(base):
    from pipeline.repo_pr import capture_patch
    from pipeline.repo_sync import ensure_thread_checkout

    one = ensure_thread_checkout(TEAM_A, THREAD_1, REPO)
    two = ensure_thread_checkout(TEAM_A, THREAD_2, REPO)
    (one / "app.py").write_text("print('one')\n")
    (two / "app.py").write_text("print('two')\n")

    patch = capture_patch(TEAM_A, REPO, THREAD_1)
    assert "print('one')" in patch
    assert "print('two')" not in patch, (
        "one thread's proposal must never carry another thread's edits"
    )


def test_a_thread_that_changed_nothing_has_nothing_to_propose(base):
    from pipeline.repo_pr import PullRequestError, capture_patch
    from pipeline.repo_sync import ensure_thread_checkout

    ensure_thread_checkout(TEAM_A, THREAD_1, REPO)
    with pytest.raises(PullRequestError):
        capture_patch(TEAM_A, REPO, THREAD_1)


def test_a_broken_checkout_never_reaches_the_repository_above_it(base, tmp_path):
    """🔴 Observed live: `fatal: Unable to create 'C:/Users/ricky/.git/index.lock'`.

    The guard was `(checkout / '.git').exists()`. A MALFORMED .git satisfies
    that while git itself rejects it and walks UP the tree looking for a real
    one — and the workspaces root sits under the user's home directory, which
    on a developer's machine is itself a repository. So `git add -A` ran
    against the user's own home repo and started staging it.
    """
    from pipeline.repo_pr import PullRequestError, capture_patch
    from pipeline.repo_sync import ensure_thread_checkout

    tree = ensure_thread_checkout(TEAM_A, THREAD_1, REPO)
    # A worktree's .git is a read-only FILE pointing at the base repository's
    # bookkeeping. Replace it with one that points nowhere — the malformed
    # state git rejects while Path.exists() happily returns True.
    pointer = tree / ".git"
    pointer.chmod(0o600)
    pointer.unlink()
    pointer.write_text("gitdir: /nowhere/that/exists\n")

    with pytest.raises(PullRequestError):
        capture_patch(TEAM_A, REPO, THREAD_1)


def test_the_base_checkout_is_still_where_the_push_happens(base):
    """open_pull_request applies an APPROVED patch onto a fresh base. It must
    keep using the team checkout — the thread's tree is the source of the
    patch, never the thing that is pushed."""
    assert repo_checkout(TEAM_A, REPO) == base
