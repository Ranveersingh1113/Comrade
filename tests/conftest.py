import os
import sys
import subprocess

import psycopg
import pytest

from shared.config import settings
from tests._seed import cleanup, seed

# Git for Windows ships `core.fsmonitor = true` in its SYSTEM gitconfig, so
# every git command in every repository starts a `git fsmonitor--daemon
# --detach`: a background process that outlives the command by design and
# holds ~37MB of commit charge.
#
# The repo tests build a fresh git repository per test in a fresh tmp_path, so
# that is one abandoned daemon per test, and the directory they were watching
# is deleted out from under them. 418 accumulated here in two hours -- 15GB of
# commit charge -- until the machine sat half a gigabyte under its commit
# limit and an unrelated pytest run died with a MemoryError that pointed
# nowhere near git.
#
# The GIT_CONFIG_* environment form reaches EVERY git subprocess, including
# the plain `git init` calls in fixtures. Production code carries the same
# setting explicitly as repo_sync.GIT_FLAGS, since Comrade's process must not
# depend on an environment variable for this.
os.environ["GIT_CONFIG_COUNT"] = "1"
os.environ["GIT_CONFIG_KEY_0"] = "core.fsmonitor"
os.environ["GIT_CONFIG_VALUE_0"] = "false"


@pytest.fixture(autouse=True)
def _sweeps_are_due():
    """Pipeline sweeps run on their own clock (T12), and that clock is module
    state — so one test that ticks would otherwise silence the sweep for every
    test after it. Reset per test rather than remembered per test file."""
    from pipeline import worker

    worker._reset_sweep_timers()
    yield


@pytest.fixture
def seeded():
    """Fresh seed per test; cleaned up after (committed worker writes included)."""
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cleanup(cur)   # idempotent
            seed(cur)
        yield
        with conn.cursor() as cur:
            cleanup(cur)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# What the suite leaves behind
# ---------------------------------------------------------------------------
# 🔴 Nothing ever looked, which is why 418 abandoned `git fsmonitor--daemon`
# processes accumulated — 15GB of commit charge — until an unrelated pytest run
# died with a MemoryError that pointed nowhere near git. Two sandbox containers
# from a timeout test spun host CPUs for a quarter of an hour in the same way,
# and the test that created them passed, because it asserted on the dict
# run_contained returned rather than on the container.
#
# FAILS on containers, REPORTS on daemons. A container named `comrade-run-*` is
# unambiguously ours and nothing else creates one. A git daemon is not
# attributable: the developer's own editor and shell spawn them constantly, so
# failing on a count would be a coin flip. Saying the number out loud is what
# would have prompted somebody to look.

def _sandbox_containers() -> set[str]:
    try:
        out = subprocess.run(
            ["docker", "ps", "--filter", "name=comrade-run-", "--quiet"],
            capture_output=True, text=True, timeout=60,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def _git_daemons() -> int:
    if not sys.platform.startswith("win"):
        return 0
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process git -EA SilentlyContinue).Count"],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return 0
    return int(out) if out.isdigit() else 0


@pytest.fixture(scope="session", autouse=True)
def _no_leaked_processes():
    before_containers = _sandbox_containers()
    before_daemons = _git_daemons()
    yield
    leaked = _sandbox_containers() - before_containers
    after_daemons = _git_daemons()

    if after_daemons > before_daemons:
        # Reported, not failed: not attributable to this suite. See above.
        print(
            f"\n[leak check] git processes went from {before_daemons} to"
            f" {after_daemons}. Comrade's own invocations pass"
            " core.fsmonitor=false, so a large jump means something here does"
            " not."
        )
    assert not leaked, (
        f"sandbox containers survived the suite: {sorted(leaked)}."
        " A timed-out `docker run` kills the CLIENT, not the container —"
        " see agent/sandbox.py:_kill. Reclaim them with"
        " `docker kill $(docker ps -q --filter name=comrade-run-)`."
    )
