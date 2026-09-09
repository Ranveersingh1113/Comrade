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
import dataclasses
import hashlib
import logging
import subprocess
from pathlib import Path

from agent.sandbox import (
    DEPS_MOUNT, MOUNT, TOOLS, VENV, SandboxError, run_setup,
)
from shared.config import settings
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


# ---------------------------------------------------------------------------
# What we install, and with what (T08)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Recipe:
    """One way to install a project's dependencies.

    `name` is the file whose content decides the cache key; `frozen` says
    whether this recipe installs an exact locked set or resolves afresh.
    """
    name: str
    runtime: str
    command: str
    frozen: bool


#: In priority order, and LOCKFILES FIRST.
#:
#: 🔴 The previous table was `("requirements.txt", "pyproject.toml")` — Python
#: only, and it read lockfiles for hashing while installing from the manifest.
#: That is reproducibility theatre: the hash moved when the lock did, so the
#: cache looked correct while the install resolved whatever the registry served
#: that day. A lock is only reproducible if the installer is told to obey it.
#: Copied into the writable dependency volume before a node install runs.
#:
#: 🔴 (fix.md F06) `npm ci --prefix /deps` does not mean "read the manifest
#: here, install over there". `--prefix` IS the project root: npm looked for
#: /deps/package.json, found nothing, and installed nothing — while the
#: manifests sat in /workspace, which setup mounts READ-ONLY, so npm could not
#: have written node_modules beside them either. Both halves had to move.
#:
#: `--dir` does the same thing for pnpm. Copied rather than symlinked because
#: an unfrozen `npm install` REWRITES package-lock.json, and through a symlink
#: that is a write into the checkout.
NODE_MANIFESTS = ("package.json", "package-lock.json", "npm-shrinkwrap.json",
                  "pnpm-lock.yaml", "pnpm-workspace.yaml", ".npmrc")

RECIPES = (
    # 🔴 `uv sync --project /workspace` puts the environment in
    # /workspace/.venv, and setup mounts the checkout read-only — so the
    # advertised uv path could not succeed on any repository at all.
    # UV_PROJECT_ENVIRONMENT moves it onto the dependency volume, which is
    # also the venv the run phase mounts.
    #
    # --no-install-project because installing the ROOT package is a PEP 517
    # build, and a build writes into the source tree (egg-info, build/) that
    # we deliberately cannot write to. The run phase's working directory is
    # the checkout, so the project's own modules import from there.
    Recipe("uv.lock", "python",
           f"UV_PROJECT_ENVIRONMENT={VENV} {TOOLS}/bin/uv sync --frozen"
           f" --no-install-project --project {MOUNT}", frozen=True),
    # Poetry makes its own virtualenv in a cache directory by default — inside
    # the read-only rootfs, and not the venv the run phase mounts.
    # VIRTUALENVS_CREATE=false makes it install into the active one instead.
    Recipe("poetry.lock", "python",
           f"cd {MOUNT} && VIRTUAL_ENV={VENV} POETRY_VIRTUALENVS_CREATE=false"
           # `poetry sync`, not `poetry install --sync`: the flag is
           # deprecated and slated for removal, and the installer is fetched
           # fresh on every setup — so the flag disappearing under us is a
           # question of when, not whether. Pinned to >=2 below, where the
           # subcommand exists.
           f" {TOOLS}/bin/poetry sync --no-root", frozen=True),
    Recipe("requirements.txt", "python",
           f"{VENV}/bin/pip install -r {MOUNT}/requirements.txt", frozen=False),
    # pip builds in a temporary directory, so a read-only checkout is fine here.
    Recipe("pyproject.toml", "python",
           f"{VENV}/bin/pip install {MOUNT}", frozen=False),
    Recipe("package-lock.json", "node",
           f"cd {DEPS_MOUNT} && npm ci --no-audit --no-fund", frozen=True),
    Recipe("pnpm-lock.yaml", "node",
           f"cd {DEPS_MOUNT} && pnpm install --frozen-lockfile"
           f" --store-dir {DEPS_MOUNT}/.pnpm-store", frozen=True),
    Recipe("package.json", "node",
           f"cd {DEPS_MOUNT} && npm install --no-audit --no-fund", frozen=False),
)

#: Manifests we recognise but do not install.
#:
#: Named explicitly, because "no manifest" and "we do not support your
#: language" are different sentences. A Go repository HAS dependencies; saying
#: it has none invites the agent to report an import failure as the team's bug.
UNSUPPORTED = ("go.mod", "Cargo.toml", "Gemfile", "pom.xml", "build.gradle")


def recipe_for(root: Path) -> Recipe | None:
    """The first recipe this repository matches, lockfiles first."""
    for recipe in RECIPES:
        candidate = root / recipe.name
        if candidate.is_file() and candidate.stat().st_size <= MANIFEST_MAX_BYTES:
            return recipe
    return None


def unsupported_runtime(root: Path) -> str | None:
    """The manifest of a language we recognise and cannot install."""
    if recipe_for(root) is not None:
        return None
    for name in UNSUPPORTED:
        if (root / name).is_file():
            return name
    return None


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
#: "2": node manifests are copied into the dependency volume and installed
#: there, and uv is pointed at that volume's virtualenv. A volume built by "1"
#: has an empty or absent node_modules and would otherwise keep a key that
#: still matched — handed back as current forever.
#:
#: "3": every volume now has a `node_modules` directory, empty for a
#: Python-only project, because the run phase mounts that subpath into the
#: checkout for ESM resolution and Docker refuses to start a container whose
#: `volume-subpath` is absent (fix.md F46). Staged manifests are also cleared
#: before the current set is copied (F48).
#:
#: DEPLOYMENT NOTE: a volume built by "1" or "2" has no `node_modules`
#: directory, so runs against it fail to start until `install()` rebuilds it —
#: which the next repository sync does, and which this bump forces. The failure
#: is Docker refusing the mount, which `run_contained` reports as "the container
#: could not start"; it is loud rather than silent, but it is a window.
RECIPE_VERSION = "3"

#: Hashed into the key when present. A lockfile pins the RESOLVED set, so it
#: changing means the installed packages change even when the manifest that
#: named them did not.
LOCKFILES = ("uv.lock", "poetry.lock", "Pipfile.lock", "requirements.lock",
             "package-lock.json", "pnpm-lock.yaml")


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

    THE IMAGE IS PART OF IT. An unchanged manifest against a new base image is
    a different environment — the interpreter moved and every installed wheel
    was built for the old one — and keying on the manifest alone hands that
    venv back as current.

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
             f"mhash={manifest_hash(root, manifest)}",
             # The image the venv was built INSIDE. Without it an unchanged
             # manifest against a new base image reuses wheels built for the
             # old interpreter, and the cache reports it as current.
             f"image={settings.comrade_sandbox_image}"]
    for lock in LOCKFILES:
        if (root / lock).is_file():
            digest = hashlib.sha256((root / lock).read_bytes()).hexdigest()[:16]
            parts.append(f"lock={lock}:{digest}")
    if manifest == "pyproject.toml":
        parts.append(f"commit={_head_sha(root) or 'unknown'}")
    # 🔴 (fix.md F48, reopened.) EVERY STAGED NODE INPUT, from NODE_MANIFESTS
    # itself rather than a second list beside it.
    #
    # This hashed `package.json` and the lockfiles and nothing else, so adding,
    # editing or removing `.npmrc`, `npm-shrinkwrap.json` or
    # `pnpm-workspace.yaml` left the digest identical. `_install_script` exits
    # at the `.manifest` comparison when the digest matches — BEFORE the
    # cleanup that clears stale staged copies — so the stale file went on
    # applying. `.npmrc` sets the REGISTRY: that is a repository changing where
    # its packages come from, and nothing noticing.
    #
    # One list, because two lists is how this happened: a file staged into the
    # volume but absent from the key is a file that can go stale. `npm ci` also
    # refuses when package.json and its lock disagree, so the key has to see
    # both or a failing environment is handed back as current.
    for name in NODE_MANIFESTS:
        candidate = root / name
        if candidate.is_file():
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()[:16]
            parts.append(f"node={name}:{digest}")
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


def _install_script(recipe: "Recipe", digest: str) -> str:
    """Compare-then-install, in one container.

    The alternative was reading the marker from the volume first, which costs a
    second container start on every sync just to learn there is nothing to do.

    `sh -c` with a STRING, which agent/capability.py refuses everywhere else —
    and the difference is the whole reason that rule exists. This string is
    built here from a fixed template and a hex digest; nothing a model said and
    nothing from the repository is interpolated into it. The manifest name comes
    from MANIFESTS, not from the filesystem.
    """
    lines = [
        "set -e",
        f'if [ "$(cat {DEPS_MOUNT}/.manifest 2>/dev/null)" = "{digest}" ]; then',
        '  echo "dependencies already current"',
        "  exit 0",
        "fi",
        # The rootfs is read-only and every one of these tools wants somewhere
        # to put state. /tmp is the sized tmpfs; the big caches go on the
        # volume below, where they survive and are not memory.
        "export HOME=/tmp",
        # Rebuild from empty. An incremental install on top of an environment
        # built from a different manifest leaves whatever the old one pulled
        # in, so "it works here" would depend on install order and history.
        f"rm -rf {DEPS_MOUNT}/venv {DEPS_MOUNT}/node_modules {TOOLS}",
        # 🔴 (fix.md F48) The staged manifests, cleared BEFORE the current set
        # is copied in. The rebuild removed installed packages and left these
        # behind, so a `.npmrc`, a shrinkwrap or a lockfile DELETED from the
        # repository went on influencing every later install — the environment
        # fingerprint had changed and the inputs had not.
        #
        # Named files rather than the directory: /deps also holds the venv, the
        # tools and the npm cache, and a blanket delete would take resources
        # this owns deliberately.
        *[f"rm -f {DEPS_MOUNT}/{name}" for name in NODE_MANIFESTS],
        # 🔴 (fix.md F46) ALWAYS, whatever the runtime. The run phase mounts
        # this subpath into the checkout so Node's ESM resolver can find it,
        # and a `volume-subpath` that does not exist refuses to start the
        # container — so a Python-only project needs the empty directory too.
        f"mkdir -p {DEPS_MOUNT}/node_modules",
    ]

    if recipe.runtime == "node":
        # 🔴 The manifests, into the place the installer actually reads.
        # See NODE_MANIFESTS: --prefix/--dir IS the project root, and the
        # checkout they used to be read from is mounted read-only.
        #
        # `if`, not `[ -f ] && cp`: under `set -e` a false test is a failed
        # command, so the && form would abort the script on the first
        # manifest a repository happens not to have — which is most of them.
        for name in NODE_MANIFESTS:
            lines.append(f"if [ -f {MOUNT}/{name} ]; then"
                         f" cp {MOUNT}/{name} {DEPS_MOUNT}/{name}; fi")
        # On the volume, not in the tmpfs: an npm cache is hundreds of
        # megabytes and the tmpfs is memory.
        lines.append(f"export npm_config_cache={DEPS_MOUNT}/.npm-cache")
    else:
        lines.append(f"python -m venv {VENV}")
        lines.append(f"export PATH={VENV}/bin:$PATH")
        if recipe.name in ("uv.lock", "poetry.lock"):
            # The installer the lockfile belongs to, in its OWN environment.
            # Both of these SYNC — they remove what the lock does not name —
            # so an installer living in the venv it is rebuilding deletes
            # itself partway through. See TOOLS in agent/sandbox.py.
            lines.append(f"python -m venv {TOOLS}")
            lines.append(f'{TOOLS}/bin/pip install uv "poetry>=2"')

    lines += [
        # The recipe's OWN command. This used to be a two-way branch that ran
        # `pip install` whatever the project was, which is exactly what made a
        # hashed lockfile meaningless: the hash moved when the lock did, while
        # the install resolved fresh from the registry.
        recipe.command,
        # World-readable, because the run phase is a different, non-root user
        # and mounts this read-only.
        f"chmod -R a+rX {DEPS_MOUNT}",
        # LAST, and only on success: `set -e` means a failed install never
        # reaches this line, so a broken environment is never marked current.
        f'echo "{digest}" > {DEPS_MOUNT}/.manifest',
    ]
    return "\n".join(lines) + "\n"


def install(team_id: str, repo_full_name: str) -> dict:
    """Bring this checkout's dependency volume up to date with its manifest.

    Returns a small report rather than raising on a failed install: a project
    whose dependencies do not resolve is a fact about that project, and the
    team needs to be told rather than have their sync job retried three times.
    """
    root = repo_checkout(team_id, repo_full_name)
    if not root.exists():
        return {"status": "no-checkout"}

    recipe = recipe_for(root)
    if recipe is None:
        # "No manifest" and "we do not install your language" are different
        # sentences, and only one of them is true for a Go repository.
        other = unsupported_runtime(root)
        if other:
            return {"status": "unsupported", "manifest": other,
                    "detail": f"{other} projects are not installed yet."}
        return {"status": "no-manifest"}

    digest = environment_key(root, recipe.name)
    volume = deps_volume(team_id, repo_full_name)
    try:
        result = run_setup(
            ["sh", "-c", _install_script(recipe, digest)], root=root, deps=volume
        )
    except SandboxError as exc:
        logger.warning("dependency install could not start for %s: %s",
                       repo_full_name, exc)
        return {"status": "error", "detail": str(exc)}

    if result["timed_out"]:
        return {"status": "timeout", "manifest": recipe.name}
    if result["exit_code"] != 0:
        # The tail, not the head: pip prints its resolution conflict last.
        detail = (result["stderr"] or result["stdout"]).strip()[-600:]
        logger.warning("dependency install failed for %s: %s",
                       repo_full_name, detail[:200])
        return {"status": "failed", "manifest": recipe.name, "detail": detail}

    if "already current" in result["stdout"]:
        return {"status": "current", "manifest": recipe.name}
    logger.info("installed dependencies for %s from %s", repo_full_name, recipe.name)
    return {"status": "installed", "manifest": recipe.name, "frozen": recipe.frozen}


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
