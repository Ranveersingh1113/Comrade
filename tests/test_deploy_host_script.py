import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
SH = shutil.which("sh") or "C:/Program Files/Git/bin/sh.exe"


@pytest.mark.parametrize("failure", ["", "build", "migration", "lock"])
def test_deployment_orders_release_and_stops_on_failure(tmp_path, failure):
    """Run the real shell script; fake only external host operations."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (tmp_path / ".git").mkdir()
    for command in ("git", "docker", "mkdir", "chown", "stat", "flock"):
        executable = fake_bin / command
        executable.write_text(
            "#!/bin/sh\n"
            f'echo "{command} $*" >> "$DEPLOY_LOG"\n'
            + ("echo 999\n" if command == "stat" else "")
            + ("[ \"$FAILURE\" != lock ] || exit 9\n" if command == "flock" else "")
            + ('''case "$*" in
  *" build") [ "$FAILURE" != build ] || exit 7 ;;
  *"shared.migrations") [ "$FAILURE" != migration ] || exit 8 ;;
esac
''' if command == "docker" else "")
            + "exit 0\n",
            encoding="utf-8", newline="\n",
        )
        executable.chmod(0o755)
    log = tmp_path / "commands.log"
    result = subprocess.run(
        [SH, str(ROOT / "scripts/deploy_host.sh"), "abc123"],
        cwd=tmp_path,
        env={**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
             "DEPLOY_LOG": log.as_posix(), "FAILURE": failure},
        capture_output=True, text=True, timeout=30,
    )
    calls = log.read_text().splitlines()
    if failure:
        assert result.returncode != 0, calls
        assert not any(" up " in call for call in calls), calls
        if failure == "lock":
            assert not any(call.startswith("git ") for call in calls), calls
        return
    assert result.returncode == 0, result.stderr
    build = next(i for i, call in enumerate(calls) if call.endswith(" build"))
    migration = next(i for i, call in enumerate(calls) if "shared.migrations" in call)
    up = next(i for i, call in enumerate(calls) if " up " in call)
    assert build < migration < up
    assert "run --rm --no-deps -T api python -m shared.migrations" in calls[migration]
    assert any("exec -T api" in call and "/ready" in call for call in calls)
    assert "git checkout --detach --force abc123" in calls


def test_workflow_serializes_and_runs_requested_commit():
    workflow = (ROOT / ".github/workflows/deploy-pilot.yml").read_text()
    assert "git show $GITHUB_SHA:scripts/deploy_host.sh | sh -s $GITHUB_SHA" in workflow
    assert "group: deploy-pilot" in workflow
    assert "cancel-in-progress: false" in workflow
