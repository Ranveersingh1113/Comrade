"""Installing what a repository needs, without giving the agent the network.

The gap this closes: `repo_run` has no network, so a repository with a
`requirements.txt` could not install anything and `pytest` failed on an import.
The agent could WRITE a change to most real projects and not VERIFY one.

The shape is a PHASE BOUNDARY. Installing reaches the network and runs from the
sync pipeline; running the team's code does not and never will. Most of what is
worth testing here is that the boundary holds.
"""
import subprocess

from pathlib import Path
import pytest

from agent.sandbox import run_contained
from pipeline.repo_deps import (
    MANIFESTS, install, manifest_for, manifest_hash, volume_for,
)
from shared.config import settings
from shared.workspace import deps_volume, repo_checkout
from tests._seed import TEAM_A, TEAM_B


def _docker_up() -> bool:
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=30
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


#: Dependency setup now REFUSES to run without an enforced registry egress
#: policy, so a host without a proxy has nothing to integration-test. Skipped
#: with a reason rather than failed: "no policy configured" is the correct
#: state on a developer machine, and the unit test above pins the refusal.
needs_setup_proxy = pytest.mark.skipif(
    not (settings.comrade_setup_proxy_url and settings.comrade_setup_proxy_container),
    reason="no registry egress proxy configured; setup is disabled by design",
)

needs_docker = pytest.mark.skipif(
    not _docker_up(), reason="Docker is not running; the sandbox needs it"
)

REPO = "acme/app"


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "shared.config.settings.comrade_workspaces_root", str(tmp_path / "ws")
    )
    root = repo_checkout(TEAM_A, REPO)
    root.mkdir(parents=True)
    return root


@pytest.fixture
def reclaim():
    """Remove any volume a test created. A Docker volume outlives the process
    that made it, so a suite that leaves them behind fills a disk slowly enough
    that nobody connects it to the tests."""
    made: list[str] = []

    def remove(name: str) -> None:
        result = subprocess.run(
            ["docker", "volume", "rm", "-f", name], capture_output=True, timeout=60,
        )
        if result.returncode and b"No such volume" not in result.stderr:
            pytest.fail(result.stderr.decode(errors="replace"))

    def reclaim_now(name: str) -> None:
        remove(name)
        made.append(name)

    yield reclaim_now
    for name in made:
        remove(name)


# ---------------------------------------------------------------------------
# What gets installed is decided by the repository, not by anything said to it
# ---------------------------------------------------------------------------

def test_requirements_wins_over_pyproject(checkout):
    """A project carrying both usually keeps its TEST dependencies in
    requirements.txt and its packaging metadata in pyproject — and running the
    tests is the entire point of this."""
    (checkout / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert manifest_for(checkout) == "pyproject.toml"
    (checkout / "requirements.txt").write_text("pytest\n")
    assert manifest_for(checkout) == "requirements.txt"


def test_a_repo_with_no_manifest_is_not_an_error(checkout, reclaim):
    """Most repositories Comrade reads will never need this. 'no-manifest' is
    an outcome, not a failure — the stdlib case worked before this existed and
    must keep working."""
    reclaim(deps_volume(TEAM_A, REPO))
    assert manifest_for(checkout) is None
    assert install(TEAM_A, REPO)["status"] == "no-manifest"
    assert volume_for(TEAM_A, REPO) is None


def test_the_hash_follows_content_not_timestamps(checkout):
    """🔴 `sync_repo` does `git reset --hard` every turn, which rewrites mtimes
    whether or not anything changed. A timestamp-based check would reinstall
    every fifteen minutes, forever."""
    (checkout / "requirements.txt").write_text("cowsay==6.1\n")
    first = manifest_hash(checkout, "requirements.txt")
    (checkout / "requirements.txt").write_text("cowsay==6.1\n")  # rewritten
    assert manifest_hash(checkout, "requirements.txt") == first
    (checkout / "requirements.txt").write_text("cowsay==6.2\n")
    assert manifest_hash(checkout, "requirements.txt") != first


def test_an_enormous_manifest_is_ignored(checkout):
    """It is read into memory to be hashed, from a repository anyone can open
    a pull request against."""
    (checkout / "requirements.txt").write_text("x\n" * 2_000_000)
    assert manifest_for(checkout) is None


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------

def test_the_model_has_no_way_to_ask_for_the_network():
    """🔴 The property the whole design rests on.

    There is no `repo_install` tool and `run_setup` is reachable only from the
    sync pipeline. A capability the model cannot name is one it cannot be
    argued into naming — the same reason team_id is bound into session state
    rather than passed as a tool argument.
    """
    from agent.agent import root_agent
    from agent.registry import REGISTRY

    names = {t.__name__ for t in root_agent.tools}
    assert not any("install" in n or "setup" in n for n in names), names
    assert not any("install" in n or "setup" in n for n in REGISTRY), sorted(REGISTRY)

    # And nothing a tool can reach calls it.
    import agent.repo_tools as rt

    assert "run_setup" not in open(rt.__file__, encoding="utf-8").read()


def test_each_team_gets_its_own_volume_for_the_same_repository():
    """The same tenancy rule the checkout path already follows. Two teams may
    legitimately connect the same public repository, and one team's installed
    dependencies must not be the other's."""
    a = deps_volume(TEAM_A, "shared/oss")
    b = deps_volume(TEAM_B, "shared/oss")
    assert a != b
    assert a.startswith("comrade-deps-") and b.startswith("comrade-deps-")


# ---------------------------------------------------------------------------
# It actually works
# ---------------------------------------------------------------------------

@needs_docker
@needs_setup_proxy
def test_a_dependency_becomes_importable_without_ever_opening_the_network(
    checkout, reclaim
):
    """🔴 The whole point, end to end.

    Before: ModuleNotFoundError. After the setup phase: importable. And the run
    phase that imports it still cannot resolve a hostname — the network was
    open for the install and for nothing else.
    """
    (checkout / "requirements.txt").write_text("cowsay==6.1\n")
    (checkout / "use_it.py").write_text("import cowsay; print('IMPORTED')\n")
    reclaim(deps_volume(TEAM_A, REPO))

    before = run_contained(["python", "use_it.py"], root=checkout)
    assert before["exit_code"] != 0
    assert "No module named" in before["stderr"].replace("^", " ")

    assert install(TEAM_A, REPO)["status"] == "installed"

    vol = volume_for(TEAM_A, REPO)
    after = run_contained(["python", "use_it.py"], root=checkout, deps=vol)
    assert after["exit_code"] == 0, after
    assert "IMPORTED" in after["stdout"].replace("^", " ")

    offline = run_contained(
        ["python", "-c", "import socket; socket.gethostbyname('example.com')"],
        root=checkout, deps=vol,
    )
    assert offline["exit_code"] != 0, "the run phase reached the network"


@needs_docker
@needs_setup_proxy
def test_an_unchanged_manifest_is_not_reinstalled(checkout, reclaim):
    """A sync runs every fifteen minutes. Rebuilding a venv each time would
    make the worker useless for anything else."""
    (checkout / "requirements.txt").write_text("cowsay==6.1\n")
    reclaim(deps_volume(TEAM_A, REPO))

    assert install(TEAM_A, REPO)["status"] == "installed"
    assert install(TEAM_A, REPO)["status"] == "current"

    (checkout / "requirements.txt").write_text("cowsay==6.1\nsix==1.17.0\n")
    assert install(TEAM_A, REPO)["status"] == "installed"


@needs_docker
@needs_setup_proxy
def test_a_dependency_that_does_not_resolve_is_reported_not_raised(
    checkout, reclaim
):
    """🔴 A project whose dependencies do not resolve is a fact about that
    project. Raising would retry the sync three times and then fail the clone,
    taking away the tools that DO work along with the one that does not."""
    (checkout / "requirements.txt").write_text(
        "comrade-no-such-package-9f3a2b==1.0.0\n"
    )
    reclaim(deps_volume(TEAM_A, REPO))

    outcome = install(TEAM_A, REPO)
    assert outcome["status"] == "failed"
    assert "detail" in outcome
    # The tail, because pip prints its resolution failure last.
    assert "No matching distribution" in outcome["detail"]


@needs_docker
@needs_setup_proxy
def test_the_setup_phase_carries_none_of_comrades_secrets(checkout, monkeypatch):
    """🔴 The reason a network-enabled phase is survivable at all.

    `pip install` runs setup.py — arbitrary code with a network connection. It
    is survivable because there is nothing of ours inside the container to
    take. This is the same assertion test_repo_run makes about the run phase,
    and it matters more here, because here the code can also phone home.
    """
    from agent.sandbox import run_setup

    monkeypatch.setenv("COMRADE_DB_URL_ADMIN", "postgresql://root:hunter2@db/x")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "-----BEGIN RSA PRIVATE KEY-----")
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyFAKEKEY")

    result = run_setup(
        ["python", "-c", "import os; print(dict(os.environ))"],
        root=checkout, deps=deps_volume(TEAM_A, "probe/env"), timeout=120,
    )
    blob = result["stdout"] + result["stderr"]
    for secret in ("hunter2", "BEGIN RSA PRIVATE KEY", "AIzaSyFAKEKEY"):
        assert secret not in blob, f"{secret} reached the setup container"
    subprocess.run(["docker", "volume", "rm", "-f",
                    deps_volume(TEAM_A, "probe/env")], capture_output=True)


@needs_docker
@needs_setup_proxy
def test_the_setup_phase_cannot_write_to_the_checkout(checkout, reclaim):
    """A package that edits the working tree during installation is doing
    something nobody asked for, and letting it would put those edits into the
    next pull request."""
    from agent.sandbox import run_setup

    reclaim(deps_volume(TEAM_A, REPO))
    result = run_setup(
        ["sh", "-c", "echo hacked > /workspace/evil.txt"],
        root=checkout, deps=deps_volume(TEAM_A, REPO), timeout=120,
    )
    assert result["exit_code"] != 0
    assert not (checkout / "evil.txt").exists()


@needs_docker
@needs_setup_proxy
def test_the_run_phase_cannot_write_to_what_setup_installed(checkout, reclaim):
    """🔴 A claim the code made and nothing checked.

    A test suite that can write to site-packages can change what the NEXT run
    imports — which turns one compromised dependency into a persistent one,
    surviving every later turn on a volume nobody thinks to inspect. Installing
    is a separate phase with the network; it is the only thing that may write
    here.
    """
    (checkout / "requirements.txt").write_text("cowsay==6.1\n")
    reclaim(deps_volume(TEAM_A, REPO))
    assert install(TEAM_A, REPO)["status"] == "installed"

    vol = volume_for(TEAM_A, REPO)
    result = run_contained(
        ["python", "-c", "open('/deps/venv/pwned.py','w').write('x')"],
        root=checkout, deps=vol,
    )
    assert result["exit_code"] != 0
    reason = result["stderr"].lower().replace("^", " ")
    assert "read-only" in reason or "permission denied" in reason, result

    # 🔴 THE ASSERTION ABOVE IS NOT ENOUGH, and mutation testing is what said
    # so: dropping `:ro` from the mount leaves that write still failing, on
    # PERMISSIONS, because setup runs as root and the run phase is uid 10001.
    # The behavioural check therefore proves file ownership and says nothing
    # about the flag it is named after — a test passing for a reason unrelated
    # to its subject.
    #
    # The mount table is the flag itself. This goes red when `:ro` is removed.
    mounts = run_contained(
        ["python", "-c",
         "print([l for l in open('/proc/mounts') if ' /deps ' in l])"],
        root=checkout, deps=vol,
    )
    line = mounts["stdout"].replace("^", " ")
    assert "/deps" in line, mounts
    assert " ro," in line or " ro " in line, (
        f"the dependency volume is not mounted read-only: {line}"
    )


def test_nothing_installs_dependencies_automatically():
    """🔴 The policy correction, guarded.

    This ran on every sync. The privilege split was right and the trigger was
    wrong: connecting a repository is a READ consent in a member's head —
    "Comrade can see our code" — and installing its manifest unattended turns
    that into "Comrade may execute this repository's dependency graph, with
    egress". Nobody consented to the second thing.

    A policy decision with no test is a policy decision that comes back, and
    this one would come back as a one-line convenience in a sync handler.
    Installing needs an explicit per-repository opt-in first.
    """
    import ast
    from pathlib import Path

    # The IMPORT, not a substring. A first attempt searched for "install(" and
    # matched `def github_install(` — a route definition — which is the kind of
    # false positive that gets a guard deleted rather than fixed.
    root = Path(__file__).resolve().parent.parent
    for module in ("pipeline/repo_sync.py", "pipeline/worker.py",
                   "pipeline/github.py", "server/app.py", "agent/tools.py"):
        tree = ast.parse((root / module).read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        } | {
            alias.name
            for node in ast.walk(tree) if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert "pipeline.repo_deps" not in imported, (
            f"{module} imports the dependency installer. Installing must not run"
            " as a side effect of connecting or syncing a repository — it needs"
            " an explicit per-repository opt-in first."
        )


def test_setup_refuses_to_run_without_an_enforced_egress_policy(monkeypatch):
    """🔴 THE INVERSION. This file used to pin the OPPOSITE — that `run_setup`
    reached the open internet — and said whoever enforced the policy had to come
    here and change it on purpose. This is that change.

    The phase runs a repository's own build hooks as root: `pip install` runs
    setup.py, `npm install` runs postinstall scripts, and exfiltration is the
    failure that leaves no trace in a diff. It now runs on a network with no
    gateway, where the only route out is a registry proxy — so a direct IP, a
    DNS lookup, an IPv6 address, a redirect and 169.254.169.254 all fail for
    the same reason: there is nowhere to go.

    Unconfigured therefore means NO SETUP. The old behaviour was unrestricted
    egress, so any fallback would quietly be the hole again.
    """
    from agent.sandbox import SandboxError, run_setup
    from shared.config import settings

    monkeypatch.setattr(settings, "comrade_setup_proxy_url", "")
    monkeypatch.setattr(settings, "comrade_setup_proxy_container", "")
    with pytest.raises(SandboxError) as caught:
        run_setup(["pip", "install", "-r", "requirements.txt"],
                  root=Path("."), deps="probe", timeout=5)
    assert "COMRADE_SETUP_PROXY_URL" in str(caught.value)


def test_the_planned_allowlist_is_gone_now_that_a_policy_is_enforced():
    """It named a policy nothing read — a constant shaped like a control,
    enforcing nothing. Removed only now that its replacement is active, which
    is what the plan required."""
    import agent.sandbox as sandbox

    assert not hasattr(sandbox, "PLANNED_SETUP_EGRESS_ALLOWLIST")
