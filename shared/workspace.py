"""Where one team's checked-out repository lives on disk.

The tenant boundary for everything the agent does to files. `team_id` decides
the directory and nothing else does — the same rule the rest of this codebase
already follows for rows, moved to the filesystem: RLS keeps team A out of team
B's tables, and this keeps team A out of team B's working tree.

WHY THE PATH IS DERIVED, NEVER PASSED
---------------------------------------
`agent/tools.py` reads `team_id` from ADK session state because the model must
not be able to name which team it is acting for. The same argument applies here
and is stronger: a workspace path that arrives as a tool argument, or is
carried alongside `team_id` in state, is a second source of truth that can
disagree with the first. There is one function, it takes a team id, and a
mismatch is not expressible.

WHY IT IS NOT INSIDE THE COMRADE REPO
---------------------------------------
Team checkouts are other people's source code. Inside our tree they would be
seen by git, synced by whatever syncs the repo, and kept out of both only by a
.gitignore line — which is a security control one careless edit from failing.
Worse, an agent scoped to "the workspace" would then be scoped to a directory
inside Comrade, and a traversal bug becomes a path to Comrade's own secrets
rather than to another empty directory. `_assert_outside_comrade` refuses that
configuration at import rather than trusting nobody sets it.
"""
import re
import shutil
from pathlib import Path

from shared.config import settings

#: Comrade's own tree. Named so the check below can say what it is protecting.
COMRADE_ROOT = Path(__file__).resolve().parent.parent

_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class WorkspaceError(Exception):
    """A workspace could not be located or is not usable."""


def _assert_outside_comrade(root: Path) -> None:
    if root == COMRADE_ROOT or COMRADE_ROOT in root.parents or root in COMRADE_ROOT.parents:
        raise WorkspaceError(
            f"COMRADE_WORKSPACES_ROOT is {root}, which overlaps Comrade's own"
            f" tree at {COMRADE_ROOT}. Team checkouts must live somewhere else:"
            " inside, a traversal bug reaches our secrets instead of an empty"
            " directory, and the separation would rest on a .gitignore line."
        )


def workspaces_root() -> Path:
    """The parent directory holding every team's checkout.

    Resolved per call rather than at import so a test can point it somewhere
    temporary without the module having already decided.
    """
    root = Path(settings.comrade_workspaces_root).expanduser().resolve()
    _assert_outside_comrade(root)
    return root


def workspace_for(team_id: str) -> Path:
    """The directory holding this team's checkout. Does not create it.

    The team id must be a UUID, and this is not defensive theatre about the
    database: it is the only thing standing between a team id and a directory
    name. Anything that is not a UUID cannot become `..`, cannot become an
    absolute path, and cannot collide with another team's directory.
    """
    if not isinstance(team_id, str) or not _UUID.match(team_id):
        raise WorkspaceError(
            f"{team_id!r} is not a team id, so it cannot name a workspace."
        )
    return workspaces_root() / team_id.lower()


#: GitHub's own rules for an owner/repo pair, applied because repo_full_name
#: reaches this from a database column that some UI will one day let a member
#: fill in. It becomes a directory name, so it gets the same suspicion team_id
#: does.
_REPO_FULL_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$")


def repo_checkout(team_id: str, repo_full_name: str) -> Path:
    """Where one repository sits inside one team's workspace.

    A team may connect more than one repo (`github_repos` is unique on
    (team_id, repo_full_name), not on team_id), so the checkout is a level
    down rather than the workspace itself.

    The slash becomes a double underscore instead of a subdirectory: nesting
    `owner/repo` would make the owner a directory shared by every repo of
    theirs, and a repo literally named `..` — GitHub forbids it, our schema
    does not — would climb. Flattening removes the question.
    """
    name = (repo_full_name or "").strip()
    if not _REPO_FULL_NAME.match(name) or ".." in name:
        raise WorkspaceError(
            f"{repo_full_name!r} is not an owner/repo name, so it cannot name"
            " a checkout directory."
        )
    return workspace_for(team_id) / name.replace("/", "__")


def ensure_workspace(team_id: str) -> Path:
    """The team's workspace directory, created if it does not exist."""
    path = workspace_for(team_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def remove_workspace(team_id: str) -> None:
    """Delete a team's checkout — on repo disconnect, or when a team ends.

    Derived, never passed: `shutil.rmtree` on a caller-supplied path is how a
    cleanup routine deletes the wrong tree. Here the only reachable argument is
    a UUID directory under the workspaces root.
    """
    path = workspace_for(team_id)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
