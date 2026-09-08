import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
SH = shutil.which("sh") or "C:/Program Files/Git/bin/sh.exe"


@pytest.mark.parametrize("failure",
                         ["", "build", "migration", "lock",
                          "proxyconfig", "proxydown"])
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
  *"config --services")
    # The prod overlay has a caddy service, so the release's proxy checks
    # must actually run here rather than being skipped.
    printf 'api\\nfrontend\\ncaddy\\n'; exit 0 ;;
  *" build") [ "$FAILURE" != build ] || exit 7 ;;
  *" -T migrate") [ "$FAILURE" != migration ] || exit 8 ;;
  *"validate --config"*) [ "$FAILURE" != proxyconfig ] || exit 6 ;;
  *"exec -T caddy"*) [ "$FAILURE" != proxydown ] || exit 5 ;;
esac
''' if command == "docker" else "")
            + "exit 0\n",
            encoding="utf-8", newline="\n",
        )
        executable.chmod(0o755)
    # Not logged, and not real: the release retries readiness 24 times and the
    # proxy 12 times, five seconds apart, so an unfaked `sleep` turns a failure
    # case into a test timeout rather than a result.
    nap = fake_bin / "sleep"
    nap.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    nap.chmod(0o755)
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
        if failure == "proxydown":
            # 🔴 This is the case that used to report success. The proxy is
            # down AFTER activation, which the previous check could not see
            # because it spoke to the api container's own localhost.
            assert any("exec -T caddy" in call for call in calls), calls
            return
        assert not any(" up " in call for call in calls), calls
        if failure == "lock":
            assert not any(call.startswith("git ") for call in calls), calls
        return
    assert result.returncode == 0, result.stderr
    build = next(i for i, call in enumerate(calls) if call.endswith(" build"))
    migration = next(i for i, call in enumerate(calls) if "-T migrate" in call)
    up = next(i for i, call in enumerate(calls) if " up " in call)
    assert build < migration < up
    # The one-off `migrate` service, not the api image: it is the only one
    # given COMRADE_DB_URL_ADMIN, so the table owner lives in one short-lived
    # container per release rather than in three weeks-long processes.
    assert "run --rm --no-deps -T migrate" in calls[migration]
    assert any("exec -T api" in call and "/ready" in call for call in calls)
    # 🔴 Validated BEFORE activation, and checked through the public path
    # AFTER it. Neither existed: an ordinary deployment could not parse its
    # Caddyfile, and the release printed "deployed" over the resulting outage.
    validate = next(i for i, call in enumerate(calls) if "validate --config" in call)
    proxy = next(i for i, call in enumerate(calls) if "exec -T caddy" in call)
    assert validate < up < proxy, calls
    assert "git checkout --detach --force abc123" in calls


def test_workflow_serializes_and_runs_requested_commit():
    workflow = (ROOT / ".github/workflows/deploy-pilot.yml").read_text()
    assert "git show $GITHUB_SHA:scripts/deploy_host.sh | sh -s $GITHUB_SHA" in workflow
    assert "group: deploy-pilot" in workflow
    assert "cancel-in-progress: false" in workflow


def test_only_the_migration_service_is_given_the_table_owner():
    """🔴 Every service shared one `.env`, so the API and both workers carried
    the RLS-bypassing credential for the weeks they ran. shared.db now refuses
    it to any process that has not called allow_table_owner(); this keeps it
    out of their environment as well, because a credential a process cannot
    use is still one an attacker can read out of it."""
    import yaml

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    for name in ("api", "pipeline-worker", "agent-worker"):
        env = services[name].get("environment") or {}
        assert env.get("COMRADE_DB_URL_ADMIN") == "", (
            f"{name} still inherits the table owner from .env"
        )
    assert "COMRADE_DB_URL_ADMIN" not in (
        services["migrate"].get("environment") or {}
    ), "the migration job is the one service that needs the real value"
    assert services["migrate"]["profiles"] == ["migrate"], (
        "a migrator that starts with the stack races the code it migrates for"
    )
