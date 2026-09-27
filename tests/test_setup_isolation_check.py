"""fix.md F62 — the isolation check's cleanup hid failures and was overbroad.

Two defects in `scripts/setup_isolation_check.sh`, both in the same eight
lines:

* `exit "$FAILED"` at the bottom evaluates FAILED *before* the EXIT trap runs,
  so `FAILED=1` set inside cleanup went into a variable nothing read again. A
  check that could not remove a network it had attached to the production
  registry proxy printed `*** could not remove ...` and exited **0**. The
  comment above it claimed the opposite, which is how it survived F55.

* Cleanup enumerated every `comrade-isolation-check-*` network on the host and
  removed them, and `rm -f /tmp/comrade-f54-*.py` removed shared paths. Two
  checks running at once destroy each other's fixtures — and the victim fails
  as if the sandbox could not reach the registry proxy.

No Docker: `docker` is a double on PATH, which is the point. These are
assertions about the script's own bookkeeping, not about the daemon.
"""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
SH = shutil.which("sh") or "C:/Program Files/Git/bin/sh.exe"
SCRIPT = ROOT / "scripts" / "setup_isolation_check.sh"

#: A network belonging to a DIFFERENT invocation, which the double reports as
#: present on the host. The old cleanup enumerated it and removed it.
FOREIGN = "comrade-isolation-check-99999-current"

# The double answers every question the check asks, logging each call. The
# gateway lookup answers EMPTY on purpose: that takes the "no gateway; nothing
# to reach" branch, which needs no python3 and no real socket, and leaves the
# network and cleanup bookkeeping — the subject here — running in full.

DOUBLE = '''#!/bin/sh
echo "$*" >> "$DOCKER_LOG"
for a in "$@"; do last="$a"; done
case "$*" in
  version*)               echo "server 99.9" ;;
  *"IPAM.Config"*)        : ;;
  *".Internal"*)          echo true ;;
  "network create"*)      echo "$last" >> "$CREATED" ;;
  "network ls"*)          cat "$CREATED" 2>/dev/null; echo "%s" ;;
  "network inspect"*)     : ;;
  "network disconnect"*)  : ;;
  "network rm "*)
      [ "$RM_FAILS" != all ] || exit 1 ;;
  "network connect"*)     : ;;
  run*)                   echo "OK 200" ;;
esac
exit 0
''' % FOREIGN


def _posix(path) -> str:
    text = str(Path(path))
    if len(text) > 1 and text[1] == ":":
        return "/" + text[0].lower() + text[2:].replace("\\", "/")
    return text.replace("\\", "/")


def _run(tmp_path, *, rm_fails=""):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    (fake_bin / "docker").write_text(DOUBLE, encoding="utf-8", newline="\n")
    (fake_bin / "docker").chmod(0o755)
    log = tmp_path / "docker.log"
    log.write_text("", encoding="utf-8")
    env = {**os.environ,
           "DOCKER_LOG": log.as_posix(),
           "CREATED": (tmp_path / "created.log").as_posix(),
           "RM_FAILS": rm_fails}
    result = subprocess.run(
        [SH, "-c",
         f'export PATH="{_posix(fake_bin)}:$PATH"\nexec sh "$0"',
         str(SCRIPT)],
        cwd=tmp_path, env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=120,
    )
    return result, log.read_text(encoding="utf-8").splitlines()


def _networks(calls):
    return [line.split()[2] for line in calls if line.startswith("network rm ")]


def test_a_healthy_check_passes(tmp_path):
    """The control. Without it, every assertion below could be satisfied by a
    script that fails for some unrelated reason."""
    result, calls = _run(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "SETUP ISOLATION OK" in result.stdout, result.stdout
    assert len(_networks(calls)) == 2, calls


def test_a_cleanup_failure_makes_the_check_fail(tmp_path):
    """🔴 THE DEFECT. `exit "$FAILED"` is evaluated before the trap, so the
    cleanup's own verdict could not reach the exit status. This left a network
    attached to the production registry proxy and said SETUP ISOLATION OK."""
    result, calls = _run(tmp_path, rm_fails="all")

    assert "could not remove" in result.stdout, result.stdout
    assert result.returncode != 0, (
        "the cleanup reported a failure and the check still exited 0"
    )


def test_a_failing_check_keeps_its_own_status(tmp_path):
    """And the merge does not go the other way: a clean cleanup must not
    overwrite a nonzero status from the body with 0."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # The isolated-gateway network cannot be created, which is a real FAIL.
    marker = '  "network create"*)'
    assert marker in DOUBLE
    refuse = DOUBLE.replace(
        marker, '  "network create --internal -o"*) exit 1 ;;\n' + marker)
    (fake_bin / "docker").write_text(refuse, encoding="utf-8", newline="\n")
    (fake_bin / "docker").chmod(0o755)
    env = {**os.environ,
           "DOCKER_LOG": (tmp_path / "docker.log").as_posix(),
           "CREATED": (tmp_path / "created.log").as_posix(),
           "RM_FAILS": ""}

    result = subprocess.run(
        [SH, "-c", f'export PATH="{_posix(fake_bin)}:$PATH"\nexec sh "$0"',
         str(SCRIPT)],
        cwd=tmp_path, env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=120,
    )

    assert result.returncode != 0, result.stdout
    assert "SETUP ISOLATION FAILED" in result.stdout, result.stdout


def test_cleanup_touches_only_this_invocations_resources(tmp_path):
    """🔴 THE SECOND DEFECT. Cleanup listed every `comrade-isolation-check-*`
    network on the host and removed them all, so a check started while another
    was probing deleted the other's networks — and the victim reports that the
    sandbox cannot reach the registry proxy, which is a lie about the product.

    Proven at the source: the check must never ASK what other networks exist.
    """
    result, calls = _run(tmp_path)

    assert not any(line.startswith("network ls") for line in calls), (
        f"the check enumerated other invocations' networks: {calls}"
    )
    removed = _networks(calls)
    created = [line.split()[-1] for line in calls
               if line.startswith("network create")]
    assert sorted(removed) == sorted(created), (removed, created)
    # The double reports a network belonging to another invocation as present.
    assert FOREIGN not in removed, (
        f"another invocation's network was removed: {removed}"
    )
    # One prefix, and it carries this invocation's pid.
    prefixes = {name.rsplit("-", 1)[0] for name in removed}
    assert len(prefixes) == 1, removed
    assert prefixes.pop().startswith("comrade-isolation-check-"), removed


def test_the_probe_programs_live_in_a_private_directory(tmp_path):
    """`/tmp/comrade-f54-*.py` were fixed names shared by every invocation, and
    cleanup removed the glob: a second check rewrote them underneath the first
    and deleted them on its way out. Per-invocation now, and removed."""
    result, calls = _run(tmp_path)

    mounts = [word for line in calls for word in line.split()
              if ":/hop.py:ro" in word or ":/probe.py:ro" in word]
    assert mounts, calls
    for mount in mounts:
        assert "comrade-f54" not in mount, mount
        host_path = Path(mount.rsplit(":/", 1)[0])
        assert not host_path.exists(), f"{host_path} outlived the check"
