import subprocess
"""One team's agent reaches one team's checkout. Nothing else.

This is a tenant boundary, not a safety rail, and it is held to the standard
tests/test_product_read_paths.py set for the database one. Team A reading team
B's working tree is the filesystem version of the four RLS holes closed in
August — a cross-tenant read, differing only in which storage engine it goes
through. It gets the same kind of sweep and the same kind of suspicion.

Three things could break it, and each has a test rather than a comment:

  1. A path that climbs out of the workspace (`../<other-team>/...`).
  2. A team id that is not a team id, so it names something other than a
     directory under the workspaces root.
  3. A workspaces root that overlaps Comrade's own tree, which would make a
     traversal bug land on our secrets rather than on an empty directory.
"""
from pathlib import Path

import pytest

from agent.capability import ArgPolicy, CapabilityError, check_path
from shared.workspace import (
    COMRADE_ROOT, WorkspaceError, ensure_workspace, remove_workspace,
    repo_checkout, workspace_for, workspaces_root,
)

TEAM_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
TEAM_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
ANY_FILE = ArgPolicy(allow=("**",), writable=("**",))


@pytest.fixture
def workspaces(tmp_path, monkeypatch):
    """Two teams with a checkout each, rooted somewhere temporary."""
    monkeypatch.setattr(
        "shared.config.settings.comrade_workspaces_root", str(tmp_path)
    )
    a, b = ensure_workspace(TEAM_A), ensure_workspace(TEAM_B)
    (a / "app.py").write_text("team A source")
    (b / "app.py").write_text("team B source")
    (b / "secrets.txt").write_text("team B's business")
    return a, b


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------

def test_a_team_reads_its_own_checkout(workspaces):
    a, _ = workspaces
    assert check_path("app.py", ANY_FILE, root=a, writing=False)


def test_a_team_cannot_climb_into_another_teams_checkout(workspaces):
    """The direct attempt. `../<uuid>/app.py` resolves to a real file that
    really exists — nothing about it is malformed, it simply belongs to
    somebody else."""
    a, _ = workspaces
    with pytest.raises(CapabilityError, match="outside this team's workspace"):
        check_path(f"../{TEAM_B}/app.py", ANY_FILE, root=a, writing=False)


def test_a_team_cannot_write_into_another_teams_checkout(workspaces):
    a, _ = workspaces
    with pytest.raises(CapabilityError, match="outside this team's workspace"):
        check_path(f"../{TEAM_B}/app.py", ANY_FILE, root=a, writing=True)


def test_an_absolute_path_to_another_teams_checkout_is_refused(workspaces):
    """Containment is measured after resolution, so naming the other tree
    outright fails the same way climbing to it does."""
    a, b = workspaces
    with pytest.raises(CapabilityError, match="outside this team's workspace"):
        check_path(str(b / "secrets.txt"), ANY_FILE, root=a, writing=False)


def test_the_workspaces_root_itself_is_not_reachable(workspaces):
    """One level up from a checkout is the directory holding every checkout.
    Listing it would enumerate the tenants even without reading a file."""
    a, _ = workspaces
    with pytest.raises(CapabilityError):
        check_path("..", ANY_FILE, root=a, writing=False)


# ---------------------------------------------------------------------------
# Comrade's own tree is not in any workspace
# ---------------------------------------------------------------------------

def test_comrades_own_source_is_unreachable(workspaces):
    """The problem that made the first version of this design wrong.

    The capability layer originally rooted at Comrade's own directory, which
    put `agent/capability.py` — the file listing the secrets nobody may read —
    inside a writable scope. Rooting per team dissolves it: our source is not
    under any team's workspace, so there is nothing to carve out.
    """
    a, _ = workspaces
    with pytest.raises(CapabilityError, match="outside this team's workspace"):
        check_path(str(COMRADE_ROOT / "agent" / "capability.py"),
                   ANY_FILE, root=a, writing=False)


def test_a_workspaces_root_inside_comrade_is_refused(monkeypatch):
    """Refused at configuration time, not discovered at exploitation time.

    Checkouts inside our tree would be kept out of git and out of reach by a
    .gitignore line, and a traversal bug would land on Comrade's secrets
    instead of an empty directory.
    """
    monkeypatch.setattr(
        "shared.config.settings.comrade_workspaces_root",
        str(COMRADE_ROOT / "workspaces"),
    )
    with pytest.raises(WorkspaceError, match="overlaps Comrade's own tree"):
        workspaces_root()


def test_a_workspaces_root_containing_comrade_is_refused(monkeypatch):
    """The other direction: a root so wide that Comrade sits inside it."""
    monkeypatch.setattr(
        "shared.config.settings.comrade_workspaces_root", str(COMRADE_ROOT.parent)
    )
    with pytest.raises(WorkspaceError, match="overlaps Comrade's own tree"):
        workspaces_root()


# ---------------------------------------------------------------------------
# A team id is the only thing that names a workspace
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "../other-team", "..", "/etc", "C:/Windows", "team-1", "", "  ",
    "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/../bbbb",
])
def test_only_a_uuid_can_name_a_workspace(bad, tmp_path, monkeypatch):
    """The single check between a team id and a directory name.

    Not defensive theatre about the database: `team_id` reaches this function
    from ADK session state, and if anything ever lets a non-UUID through, this
    is what stops it becoming `..`.
    """
    monkeypatch.setattr(
        "shared.config.settings.comrade_workspaces_root", str(tmp_path)
    )
    with pytest.raises(WorkspaceError):
        workspace_for(bad)


def test_the_same_team_always_gets_the_same_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "shared.config.settings.comrade_workspaces_root", str(tmp_path)
    )
    assert workspace_for(TEAM_A) == workspace_for(TEAM_A.upper())


def test_removing_a_workspace_takes_only_that_team(workspaces):
    """Cleanup runs on repo disconnect and when a team ends. `rmtree` on a
    caller-supplied path is how such a routine deletes the wrong tree; here
    the only reachable argument is a UUID under the workspaces root."""
    a, b = workspaces
    remove_workspace(TEAM_A)
    assert not a.exists()
    assert (b / "app.py").read_text() == "team B source"


def test_removing_a_workspace_that_never_existed_is_quiet(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "shared.config.settings.comrade_workspaces_root", str(tmp_path)
    )
    remove_workspace(TEAM_B)  # no exception


def test_a_workspace_is_a_plain_directory(workspaces):
    """No marker file, no manifest, nothing to keep in sync. The path is
    derived from the team id, so the filesystem holds no state that could
    disagree with the database."""
    a, _ = workspaces
    assert a.is_dir()
    assert a.name == TEAM_A
    assert a.parent == Path(workspaces_root())


def test_a_git_checkout_is_actually_deleted(tmp_path, monkeypatch):
    """🔴 Silently true everywhere it mattered, for as long as this existed.

    `shutil.rmtree(path, ignore_errors=True)` does NOT delete a checkout. Git
    marks everything under .git/objects read-only, Windows refuses to unlink a
    read-only file, and ignore_errors swallows every refusal — so the call
    returns cleanly having removed the working tree and left .git behind.

    Nothing noticed. A departed team's code stayed on disk indefinitely, which
    is a retention problem rather than a tidiness one; the orphan sweep
    reported removals that had not happened; and enforce_disk_cap evicted its
    way around directories that never got smaller.

    Asserted on a REAL git repository, because an empty directory deletes
    cleanly and would have proved nothing.
    """
    monkeypatch.setattr(
        "shared.config.settings.comrade_workspaces_root", str(tmp_path / "ws")
    )
    root = repo_checkout(TEAM_A, "acme/app")
    root.mkdir(parents=True)
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", *a], cwd=root, check=True, capture_output=True
    )
    run("init", "-q")
    (root / "app.py").write_text("print('x')\n")
    run("add", "-A")
    run("-c", "user.email=t@t.dev", "-c", "user.name=T", "commit", "-qm", "x")
    assert any((root / ".git" / "objects").rglob("*"))

    remove_workspace(TEAM_A)
    assert not workspace_for(TEAM_A).exists()
