"""Whether the dependency recipes install anything at all.

🔴 THE DEFECT (fix.md F06). Four of the seven advertised recipes could not
work, and the sync pipeline is where they failed — so nobody saw it, and the
agent reported the resulting import errors as the team's bug.

  * The image is python:3.12-slim. `npm`, `pnpm` and `npx` are not in it, so
    all three node recipes died on "npm: not found".
  * `npm ci --prefix /deps` does not mean "read the manifest here, install
    over there". `--prefix` IS the project root: npm looked in /deps, found no
    package.json, and installed nothing — and the manifests could not simply
    be left where they were, because setup mounts the checkout READ-ONLY, so
    node_modules could not be written beside them either.
  * `uv sync --project /workspace` puts the environment in /workspace/.venv,
    which is that same read-only mount.

And the two places that describe the resulting environment had already
drifted: the preview set NODE_PATH, the finite-command path did not, and
neither put node_modules/.bin on PATH — so `eslint`, `vite` and `tsc` were
missing from both even when they had been installed.

WHAT THESE TESTS DO AND DO NOT COVER. The full acceptance — real npm, pnpm and
uv projects installed through the real setup container — needs the registry
egress proxy, and is marked as needing it. Everything below is about the
argv, the script, and the image, which is what was actually wrong.
"""
import subprocess
import uuid

import pytest

from agent.sandbox import DEPS_MOUNT, MOUNT, VENV, deps_env
from pipeline.repo_deps import (
    NODE_MANIFESTS, RECIPES, _install_script, environment_key, recipe_for,
)
from shared.config import settings


def _docker_up() -> bool:
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=30,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


needs_image = pytest.mark.skipif(
    not _docker_up(), reason="Docker is not running; the image cannot be inspected",
)


def _script_for(name: str, root) -> str:
    (root / name).write_text("{}", encoding="utf-8")
    recipe = recipe_for(root)
    assert recipe is not None and recipe.name == name
    return _install_script(recipe, "d" * 32)


# ---------------------------------------------------------------------------
# The image has what the recipes call
# ---------------------------------------------------------------------------

@needs_image
@pytest.mark.parametrize("tool", ["node", "npm", "pnpm", "npx"])
def test_the_image_has_the_runtimes_the_recipes_invoke(tool):
    """🔴 The plainest half of the finding. Three recipes named npm and pnpm;
    the image was python:3.12-slim."""
    result = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh",
         settings.comrade_sandbox_image, "-c", f"command -v {tool}"],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, (
        f"{tool} is not in {settings.comrade_sandbox_image}:"
        f" {result.stderr.strip()[:200]}"
    )


@needs_image
def test_the_python_tooling_the_image_already_promised_is_still_there():
    """Adding a runtime must not cost the ones repo_run's allowlist names."""
    result = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh",
         settings.comrade_sandbox_image, "-c",
         "command -v pytest && command -v ruff && command -v mypy"],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stderr.strip()[:300]


@needs_image
def test_the_image_still_runs_as_a_non_root_user():
    result = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh",
         settings.comrade_sandbox_image, "-c", "id -u"],
        capture_output=True, text=True, timeout=180,
    )
    assert result.stdout.strip() == "10001"


# ---------------------------------------------------------------------------
# The install writes where it is allowed to write
# ---------------------------------------------------------------------------

def test_a_node_install_gets_the_manifests_it_reads(tmp_path):
    """🔴 The finding's own case. npm's project root is the directory it is
    pointed at; the manifests were in the other one, which is read-only."""
    script = _script_for("package-lock.json", tmp_path)

    assert f"cp {MOUNT}/package.json {DEPS_MOUNT}/package.json" in script
    assert f"cp {MOUNT}/package-lock.json {DEPS_MOUNT}/package-lock.json" in script


def test_the_node_install_runs_in_the_writable_volume(tmp_path):
    script = _script_for("package-lock.json", tmp_path)

    assert f"cd {DEPS_MOUNT} && npm ci" in script
    assert "--prefix" not in script, (
        "--prefix sets the project root; it does not redirect the output"
    )


def test_a_missing_optional_manifest_does_not_abort_the_install(tmp_path):
    """`set -e` is at the top of this script, so `[ -f x ] && cp x y` aborts it
    on the first manifest a repository happens not to have — which is most of
    them, for every repository."""
    script = _script_for("package-lock.json", tmp_path)

    for line in script.splitlines():
        if line.startswith("if [ -f") and "cp " in line:
            assert line.endswith("fi"), f"not a complete if: {line}"
    assert "] && cp" not in script


def test_uv_installs_into_the_deps_volume_not_the_read_only_checkout(tmp_path):
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    recipe = recipe_for(tmp_path)

    assert recipe is not None and recipe.name == "uv.lock"
    assert f"UV_PROJECT_ENVIRONMENT={VENV}" in recipe.command
    # Building the root package writes egg-info into the source tree.
    assert "--no-install-project" in recipe.command


def test_poetry_installs_into_the_active_venv_rather_than_its_own(tmp_path):
    (tmp_path / "poetry.lock").write_text("# lock\n", encoding="utf-8")
    recipe = recipe_for(tmp_path)

    assert recipe is not None
    assert "POETRY_VIRTUALENVS_CREATE=false" in recipe.command
    assert f"VIRTUAL_ENV={VENV}" in recipe.command


def test_no_recipe_writes_into_the_checkout(tmp_path):
    """The read-only mount is the contract; a recipe that needs to break it is
    a recipe that fails, and it must not be advertised as working."""
    for recipe in RECIPES:
        for fragment in (f"{MOUNT}/.venv", f"--prefix {MOUNT}",
                         f"--dir {MOUNT}", f"--target {MOUNT}"):
            assert fragment not in recipe.command, (
                f"{recipe.name} installs into the read-only checkout"
            )


def test_a_python_recipe_does_not_copy_node_manifests(tmp_path):
    """It still CLEARS them, though — a repository that moved from Node to
    Python should not leave a stale package-lock.json staged in its volume
    (fix.md F48). What it must not do is stage a new one."""
    script = _script_for("requirements.txt", tmp_path)

    assert f"cp {MOUNT}/package.json" not in script
    assert f"rm -f {DEPS_MOUNT}/package.json" in script
    assert f"python -m venv {VENV}" in script


def test_the_marker_is_written_only_after_a_successful_install(tmp_path):
    """A volume marked current with a failed install is one nothing will ever
    rebuild."""
    script = _script_for("package-lock.json", tmp_path)
    lines = [line for line in script.splitlines() if line.strip()]

    assert lines[0] == "set -e"
    assert ".manifest" in lines[-1]
    assert "npm ci" in "\n".join(lines[:-1])


# ---------------------------------------------------------------------------
# The fingerprint follows what was installed
# ---------------------------------------------------------------------------

def test_a_changed_node_lockfile_changes_the_environment_key(tmp_path):
    (tmp_path / "package.json").write_text('{"name":"a"}', encoding="utf-8")
    (tmp_path / "package-lock.json").write_text('{"lockfileVersion":3}',
                                                encoding="utf-8")
    before = environment_key(tmp_path, "package-lock.json")

    (tmp_path / "package-lock.json").write_text(
        '{"lockfileVersion":3,"packages":{}}', encoding="utf-8")

    assert environment_key(tmp_path, "package-lock.json") != before


def test_a_changed_package_json_changes_it_too(tmp_path):
    """`npm ci` refuses when the lock and the manifest disagree, so a
    package.json that moved alone is a different — failing — environment."""
    (tmp_path / "package.json").write_text('{"name":"a"}', encoding="utf-8")
    (tmp_path / "package-lock.json").write_text('{"lockfileVersion":3}',
                                                encoding="utf-8")
    before = environment_key(tmp_path, "package-lock.json")

    (tmp_path / "package.json").write_text('{"name":"a","version":"2"}',
                                           encoding="utf-8")

    assert environment_key(tmp_path, "package-lock.json") != before


def test_the_recipe_version_moved_with_the_layout(tmp_path):
    """A volume built by the OLD script has no node_modules in it. Without a
    version bump its key still matches and it is handed back as current."""
    from pipeline.repo_deps import RECIPE_VERSION

    assert RECIPE_VERSION != "1"


# ---------------------------------------------------------------------------
# One environment, not two that drift
# ---------------------------------------------------------------------------

def test_the_run_and_the_preview_describe_the_same_environment():
    """🔴 They were two copies, and they had already diverged: the preview set
    NODE_PATH and the finite-command path did not, so the same volume gave
    `node -e "require(...)"` two different answers."""
    from agent.processes import _run_argv
    from agent.sandbox import _docker_run_argv
    from pathlib import Path

    run = _docker_run_argv(["pytest"], root=Path("/tmp/ws"), deps="v", name="n")
    preview = _run_argv("n", Path("/tmp/ws"), "npm start", 3000, "net", "v")

    def env(argv):
        return {argv[i + 1] for i, a in enumerate(argv)
                if a == "-e" and argv[i + 1].split("=")[0]
                in ("PATH", "VIRTUAL_ENV", "NODE_PATH")}

    assert env(run) == env(preview)


def test_installed_binaries_are_on_the_path():
    """eslint, vite, tsc, jest — a project's tools live in node_modules/.bin,
    and neither path had it, so every one of them was missing from a container
    that had just installed it."""
    path = next(v for v in deps_env("vol") if v.startswith("PATH="))

    # Under the CHECKOUT now, not /deps: F46 moved the mount to where Node's
    # ESM resolver looks, and PATH has to follow it or the binaries point at a
    # directory nothing is mounted at.
    assert f"{MOUNT}/node_modules/.bin" in path
    assert path.index(f"{VENV}/bin") < path.index("/usr/bin"), (
        "the image's own tools would win over the ones the project pinned"
    )


def test_a_repository_without_dependencies_mounts_nothing():
    assert deps_env(None) == []


def test_the_dependency_volume_is_still_read_only():
    assert f"vol:{DEPS_MOUNT}:ro" in deps_env("vol")


def test_every_node_manifest_copied_is_one_an_installer_reads():
    """A list that grows by habit ends up copying the repository into the
    volume. Each of these is read by npm or pnpm from the project root."""
    assert set(NODE_MANIFESTS) <= {
        "package.json", "package-lock.json", "npm-shrinkwrap.json",
        "pnpm-lock.yaml", "pnpm-workspace.yaml", ".npmrc",
    }

# ---------------------------------------------------------------------------
# F46 — where Node actually looks
# ---------------------------------------------------------------------------

def test_dependencies_are_mounted_where_module_resolution_looks():
    """🔴 (fix.md F46) Packages lived in /deps/node_modules and execution
    relied on NODE_PATH. Node's ESM resolver IGNORES NODE_PATH — it walks up
    from the importing file looking for `node_modules` — so a successful
    install still left every modern Node app failing with
    ERR_MODULE_NOT_FOUND. Measured in an isolated no-network container:
    `require` returned a function, `import` threw."""
    mounts = [v for v in deps_env("vol") if v.startswith("type=volume")]

    assert mounts, "nothing is mounted inside the checkout for resolution"
    spec = mounts[0]
    assert f"target={MOUNT}/node_modules" in spec
    assert "volume-subpath=node_modules" in spec
    # The run phase uses what setup installed and never adds to it.
    assert "readonly" in spec


def test_the_paths_point_at_the_checkouts_node_modules():
    """PATH and NODE_PATH have to follow the mount, or a project's pinned
    binaries and its CommonJS requires point at a directory nothing is at."""
    env = deps_env("vol")
    path = next(v for v in env if v.startswith("PATH="))
    node_path = next(v for v in env if v.startswith("NODE_PATH="))

    assert f"{MOUNT}/node_modules/.bin" in path
    assert node_path == f"NODE_PATH={MOUNT}/node_modules"


def test_every_volume_gets_a_node_modules_directory(tmp_path):
    """Even a Python-only project. Docker REFUSES to start a container whose
    `volume-subpath` is absent — measured — so the directory has to exist on
    every volume this system builds, empty or not."""
    script = _script_for("requirements.txt", tmp_path)

    assert f"mkdir -p {DEPS_MOUNT}/node_modules" in script


def test_the_recipe_version_moved_with_this_layout_too():
    """A volume built by an earlier layout has no node_modules, so a run
    against it cannot start. The bump is what forces the rebuild."""
    from pipeline.repo_deps import RECIPE_VERSION

    assert RECIPE_VERSION not in ("1", "2")


# ---------------------------------------------------------------------------
# F48 — the staged manifests are the current ones
# ---------------------------------------------------------------------------

def test_a_rebuild_clears_the_previously_staged_manifests(tmp_path):
    """🔴 (fix.md F48) The rebuild removed installed packages and left the
    staged manifests behind, so a `.npmrc`, a shrinkwrap or a lockfile DELETED
    from the repository went on influencing every later install — the
    environment fingerprint had changed and the inputs had not."""
    script = _script_for("package-lock.json", tmp_path)
    lines = script.splitlines()

    removals = [i for i, line in enumerate(lines)
                if line.startswith(f"rm -f {DEPS_MOUNT}/")]
    copies = [i for i, line in enumerate(lines) if line.startswith("if [ -f ")]

    assert removals, "nothing clears the previously staged manifests"
    assert copies, "nothing stages the current ones"
    assert max(removals) < min(copies), (
        "the current manifests are copied before the stale ones are cleared"
    )


def test_the_removals_name_files_rather_than_the_directory(tmp_path):
    """/deps also holds the venv, the installer tools and the npm cache. A
    blanket delete would take resources this owns deliberately."""
    script = _script_for("package-lock.json", tmp_path)

    assert f"rm -rf {DEPS_MOUNT}\n" not in script
    assert f"rm -f {DEPS_MOUNT}/.npmrc" in script
    for owned in ("venv", "tools", ".npm-cache"):
        assert f"rm -f {DEPS_MOUNT}/{owned}" not in script


def test_every_staged_manifest_is_also_cleared(tmp_path):
    """A file that can be copied in and not cleared is a file that can go
    stale, so the two lists have to be the same list."""
    script = _script_for("package-lock.json", tmp_path)

    for name in NODE_MANIFESTS:
        assert f"rm -f {DEPS_MOUNT}/{name}" in script, f"{name} is never cleared"


@needs_image
def test_both_commonjs_and_esm_resolve_through_the_real_paths(tmp_path):
    """The acceptance, run for real: one installed fixture, imported both ways,
    through the argv the product itself builds.

    Marked as needing the image because it installs a package and starts
    containers. The unit checks above are the fast net; this is the evidence,
    and its absence is what let F46 ship.
    """
    import json
    from pathlib import Path as _Path

    from agent.sandbox import _docker_run_argv
    from pipeline.repo_deps import _install_script, recipe_for

    root = tmp_path / "app"
    root.mkdir()
    (root / "package.json").write_text(json.dumps({
        "name": "probe", "version": "1.0.0", "private": True,
        "dependencies": {"leftpad": "0.0.1"},
    }), encoding="utf-8")
    (root / "probe.cjs").write_text(
        "console.log('CJS:', typeof require('leftpad'));\n", encoding="utf-8")
    (root / "probe.mjs").write_text(
        "import leftpad from 'leftpad';\n"
        "console.log('ESM:', typeof leftpad);\n", encoding="utf-8")

    volume = f"comrade-test-{uuid.uuid4().hex[:10]}"
    try:
        # A lockfile, so the frozen recipe is the one under test.
        subprocess.run(
            ["docker", "run", "--rm", "-v", f"{root}:/out", "-w", "/out",
             "--user", "0:0", settings.comrade_sandbox_image, "sh", "-c",
             "export HOME=/tmp; npm install --package-lock-only"
             " --no-audit --no-fund"],
            capture_output=True, timeout=300, check=True,
        )
        recipe = recipe_for(root)
        assert recipe is not None and recipe.name == "package-lock.json"

        installed = subprocess.run(
            ["docker", "run", "--rm", "--user", "0:0", "--read-only",
             "--tmpfs", "/tmp:size=1024m",
             "-v", f"{root}:{MOUNT}:ro", "-v", f"{volume}:{DEPS_MOUNT}",
             "-w", MOUNT, settings.comrade_sandbox_image,
             "sh", "-c", _install_script(recipe, "probe-digest")],
            capture_output=True, text=True, timeout=600,
        )
        assert installed.returncode == 0, installed.stderr[-500:]

        ran = subprocess.run(
            _docker_run_argv(
                ["sh", "-c", "node probe.cjs; node probe.mjs"],
                root=root, deps=volume, name=f"comrade-run-{uuid.uuid4().hex[:8]}",
            ),
            capture_output=True, text=True, timeout=300,
        )
        output = ran.stdout + ran.stderr
        assert "CJS: function" in output, output[-600:]
        assert "ESM: function" in output, (
            "ESM resolution still fails, which is the whole of F46:"
            f" {output[-600:]}"
        )
    finally:
        subprocess.run(["docker", "volume", "rm", "-f", volume],
                       capture_output=True, timeout=120)
