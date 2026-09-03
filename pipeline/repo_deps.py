"""Installing what a repository needs, so its own tests can run.

Phase F. `repo_run` has no network, which is the control that does the most
work in agent/sandbox.py and is not negotiable. But it meant that a repository
with a `requirements.txt` could not install anything, so `pytest` failed on an
import and the agent could WRITE a change to most real projects without being
able to VERIFY one. A coding agent that cannot run the project's tests is doing
something meaningfully weaker than the word "harness" implies.

THE SHAPE IS A PHASE BOUNDARY, NOT A FLAG
-------------------------------------------
Installing needs the network; running the team's code must not have it. Those
are different phases with different privileges, and collapsing them into a
`network=True` argument on one function would put the decision at a call site
where somebody will eventually pass the wrong value. So: `run_setup` reaches
the network, runs from here, and is not reachable from any tool. `run_contained`
never does.

THE MODEL CANNOT ASK FOR THIS
-------------------------------
There is deliberately no `repo_install` tool. Installation happens on sync,
from the repository's own manifest, decided by the manifest rather than by
anything the agent says. A capability the model cannot name is one it cannot be
argued into naming — the same reason `team_id` is bound into session state
rather than passed as a tool argument.

WHAT IS HONESTLY NOT SOLVED
-----------------------------
`pip install` runs `setup.py`. Installing a dependency is arbitrary code
execution with a network connection, here and in every CI system there has ever
been. The container is the boundary and nothing of Comrade's is inside it.
"""
import hashlib
import logging
from pathlib import Path

from agent.sandbox import DEPS_MOUNT, MOUNT, VENV, SandboxError, run_setup
from shared.workspace import deps_volume, repo_checkout

logger = logging.getLogger(__name__)

#: Checked in order; the first found decides how to install. requirements.txt
#: comes first because a project carrying both usually keeps its TEST
#: dependencies there and its packaging metadata in pyproject — and running the
#: tests is the point of this.
MANIFESTS = ("requirements.txt", "pyproject.toml")

#: A manifest bigger than this is not a manifest. Bounded because it is read
#: into memory to be hashed, from a repository anyone can open a PR against.
MANIFEST_MAX_BYTES = 1_000_000


def manifest_for(root: Path) -> str | None:
    """Which manifest this repository has, if any."""
    for name in MANIFESTS:
        candidate = root / name
        if candidate.is_file() and candidate.stat().st_size <= MANIFEST_MAX_BYTES:
            return name
    return None


def manifest_hash(root: Path, name: str) -> str:
    """What we install FROM, so a re-sync only re-installs when it changed.

    Hashing the file rather than trusting a timestamp: `git reset --hard` on
    every sync rewrites mtimes whether or not the content moved, so a
    timestamp would reinstall every fifteen minutes forever.
    """
    return hashlib.sha256((root / name).read_bytes()).hexdigest()[:32]


def _install_script(name: str, digest: str) -> str:
    """Compare-then-install, in one container.

    The alternative was reading the marker from the volume first, which costs a
    second container start on every sync just to learn there is nothing to do.

    `sh -c` with a STRING, which agent/capability.py refuses everywhere else —
    and the difference is the whole reason that rule exists. This string is
    built here from a fixed template and a hex digest; nothing a model said and
    nothing from the repository is interpolated into it. The manifest name comes
    from MANIFESTS, not from the filesystem.
    """
    return (
        "set -e\n"
        f'if [ "$(cat {DEPS_MOUNT}/.manifest 2>/dev/null)" = "{digest}" ]; then\n'
        '  echo "dependencies already current"\n'
        "  exit 0\n"
        "fi\n"
        # Rebuild from empty. An incremental install on top of a venv built
        # from a different manifest leaves whatever the old one pulled in, so
        # "it works here" would depend on install order and history.
        f"rm -rf {DEPS_MOUNT}/venv\n"
        f"python -m venv {VENV}\n"
        + (
            f"{VENV}/bin/pip install -r {MOUNT}/requirements.txt\n"
            if name == "requirements.txt"
            else f"{VENV}/bin/pip install {MOUNT}\n"
        )
        # World-readable, because the run phase is a different, non-root user
        # and mounts this read-only.
        + f"chmod -R a+rX {DEPS_MOUNT}\n"
        f'echo "{digest}" > {DEPS_MOUNT}/.manifest\n'
    )


def install(team_id: str, repo_full_name: str) -> dict:
    """Bring this checkout's dependency volume up to date with its manifest.

    Returns a small report rather than raising on a failed install: a project
    whose dependencies do not resolve is a fact about that project, and the
    team needs to be told rather than have their sync job retried three times.
    """
    root = repo_checkout(team_id, repo_full_name)
    if not root.exists():
        return {"status": "no-checkout"}

    name = manifest_for(root)
    if name is None:
        return {"status": "no-manifest"}

    digest = manifest_hash(root, name)
    volume = deps_volume(team_id, repo_full_name)
    try:
        result = run_setup(
            ["sh", "-c", _install_script(name, digest)], root=root, deps=volume
        )
    except SandboxError as exc:
        logger.warning("dependency install could not start for %s: %s",
                       repo_full_name, exc)
        return {"status": "error", "detail": str(exc)}

    if result["timed_out"]:
        return {"status": "timeout", "manifest": name}
    if result["exit_code"] != 0:
        # The tail, not the head: pip prints its resolution conflict last.
        detail = (result["stderr"] or result["stdout"]).strip()[-600:]
        logger.warning("dependency install failed for %s: %s",
                       repo_full_name, detail[:200])
        return {"status": "failed", "manifest": name, "detail": detail}

    if "already current" in result["stdout"]:
        return {"status": "current", "manifest": name}
    logger.info("installed dependencies for %s from %s", repo_full_name, name)
    return {"status": "installed", "manifest": name}


def volume_for(team_id: str, repo_full_name: str) -> str | None:
    """The dependency volume to mount for a run, or None if there is nothing
    installed. Callers pass this straight to run_contained's `deps`."""
    root = repo_checkout(team_id, repo_full_name)
    if not root.exists() or manifest_for(root) is None:
        return None
    return deps_volume(team_id, repo_full_name)
