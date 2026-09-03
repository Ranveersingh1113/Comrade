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
There is deliberately no `repo_install` tool, and there will not be one. What
gets installed is decided by the repository's own manifest and by a member's
explicit opt-in, never by anything the agent says. A capability the model
cannot name is one it cannot be argued into naming — the same reason `team_id`
is bound into session state rather than passed as a tool argument.

NOTHING CALLS THIS AUTOMATICALLY, AND THAT WAS A CORRECTION
------------------------------------------------------------
It ran on every sync. The privilege split above was right and the trigger was
wrong: connecting a repository is a READ consent in a member's head — "Comrade
can see our code" — and installing its manifest unattended silently turns that
into "Comrade may execute this repository's dependency graph, with egress".
Nobody consented to the second thing. In a product whose thesis is that actions
are proposed and approved, an execute-with-network capability arriving as a side
effect of a checkbox is the one shape that cannot be defended.

So `install()` is called by nothing today. It needs an explicit per-repository
opt-in before it is wired to anything.

TWO THINGS TO FIX BEFORE IT IS
--------------------------------
1. THE CACHE KEY IS WRONG for the pyproject path. It is the manifest hash
   alone, but `pip install /workspace` installs the repository's OWN package —
   so a commit that changes source without touching pyproject.toml leaves a
   stale build installed and tests running against code that is not in the
   checkout. The key needs the commit SHA, the lockfile when there is one, and
   a recipe version so changing `_install_script` invalidates what it built.

2. THERE IS NO VISIBLE STATUS. A member cannot see whether an environment is
   ready, building, failed, stale or disabled — and neither can the agent, so
   it cannot tell a member whether a red test suite is their code or a missing
   environment. That distinction is the main thing this feature is for.

WHAT IS HONESTLY NOT SOLVED
-----------------------------
`pip install` runs `setup.py`. Installing a dependency is arbitrary code
execution with a network connection, here and in every CI system there has ever
been. The container is the boundary and nothing of Comrade's is inside it.
"""
import hashlib
import logging
import subprocess
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


#: Bump when `_install_script` changes what it builds. Without it, a volume
#: built by an older recipe keeps a key that still matches and is never
#: rebuilt — the environment silently keeps whatever layout the old script
#: produced, which is the kind of staleness that surfaces as an inexplicable
#: import error months later.
RECIPE_VERSION = "1"

#: Hashed into the key when present. A lockfile pins the RESOLVED set, so it
#: changing means the installed packages change even when the manifest that
#: named them did not.
LOCKFILES = ("uv.lock", "poetry.lock", "Pipfile.lock", "requirements.lock")


def _head_sha(root: Path) -> str | None:
    """The commit this checkout is on, or None if that cannot be read."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "HEAD"],
            cwd=str(root), capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def environment_key(root: Path, manifest: str) -> str:
    """What an environment was built FROM. Comparing it is how staleness is
    answered without storing a second copy of it.

    🔴 THE COMMIT IS IN THE KEY ONLY WHEN THE REPOSITORY'S OWN PACKAGE IS
    INSTALLED, and that asymmetry is the point rather than an oversight.

    The pyproject path runs `pip install /workspace`, which installs the repo
    itself — so a commit that changes source without touching pyproject.toml
    leaves a STALE BUILD installed, and the tests then run against code that is
    not in the checkout. Keying on the manifest alone, as the first version
    did, makes that invisible.

    The requirements.txt path installs only third-party packages, so folding
    the commit in would rebuild a venv on every push and never change a byte of
    what it contains. A cache key that is too specific is not merely wasteful;
    an environment that rebuilds for three minutes on every commit is one a
    team turns off.
    """
    parts = [f"recipe={RECIPE_VERSION}", f"manifest={manifest}",
             f"mhash={manifest_hash(root, manifest)}"]
    for lock in LOCKFILES:
        if (root / lock).is_file():
            digest = hashlib.sha256((root / lock).read_bytes()).hexdigest()[:16]
            parts.append(f"lock={lock}:{digest}")
    if manifest == "pyproject.toml":
        parts.append(f"commit={_head_sha(root) or 'unknown'}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def has_lockfile(root: Path) -> str | None:
    """Which lockfile this repository pins with, if any. Surfaced rather than
    enforced: an unhashed requirements.txt is the norm in Python, and refusing
    to build without a lockfile would exclude most repositories that need
    this. A deployment that wants to require one can, on this."""
    for lock in LOCKFILES:
        if (root / lock).is_file():
            return lock
    return None


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
    """The dependency volume to mount for a run, or None if none is PROVISIONED.

    Checks the volume exists rather than checking the repository has a
    manifest. Those were the same thing while installs happened automatically
    on sync; they are not now. `docker run -v name:/deps` CREATES `name` when
    it is absent, so returning a name for an environment nobody built would
    mount an empty volume, put a nonexistent venv on PATH, and leave the agent
    reporting import errors for a setup that was never asked for.

    Only `install()` creates one, so existence is the readiness signal until
    there is a status column to ask instead.
    """
    name = deps_volume(team_id, repo_full_name)
    try:
        found = subprocess.run(  # noqa: S603 - fixed argv
            ["docker", "volume", "inspect", name],
            capture_output=True, timeout=30,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return None
    return name if found else None
