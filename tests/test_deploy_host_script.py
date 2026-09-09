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
    # The helper, where a real checkout puts it. The release resolves it from
    # the working tree rather than from `$0`, because the workflow pipes this
    # script in on stdin and `$0` is then `sh` (fix.md F35).
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "proxy_check.sh",
                tmp_path / "scripts" / "proxy_check.sh")
    for command in ("git", "docker", "mkdir", "chown", "stat", "flock", "curl"):
        executable = fake_bin / command
        executable.write_text(
            "#!/bin/sh\n"
            f'echo "{command} $*" >> "$DEPLOY_LOG"\n'
            + ("echo 999\n" if command == "stat" else "")
            + ("[ \"$FAILURE\" != lock ] || exit 9\n" if command == "flock" else "")
            # 🔴 The public check no longer speaks through the caddy container.
            # `exec -T caddy wget https://localhost/...` asked for a hostname
            # Caddy has no site for, and disabled certificate verification while
            # it was there (fix.md F35). It now runs scripts/proxy_check.sh on
            # the host, so `curl` is the external operation to fake — and the
            # real script runs.
            + ("[ \"$FAILURE\" != proxydown ] || exit 7\n"
               if command == "curl" else "")
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
             "DEPLOY_LOG": log.as_posix(), "FAILURE": failure,
             # The configured public name. Without it the release now refuses
             # to claim the site is served rather than asking localhost, which
             # is the whole of fix.md F35.
             "COMRADE_HOST": "comrade.example.test"},
        capture_output=True, text=True, timeout=30,
    )
    calls = log.read_text().splitlines()
    if failure:
        assert result.returncode != 0, calls
        if failure == "proxydown":
            # 🔴 This is the case that used to report success. The proxy is
            # down AFTER activation, which the api container's own localhost
            # could not see — and which the caddy-container check could not see
            # either, because it asked for a hostname with no site (F35).
            assert any("curl" in call and "comrade.example.test" in call
                       for call in calls), calls
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
    proxy = next(i for i, call in enumerate(calls) if call.startswith("curl "))
    assert validate < up < proxy, calls
    # The CONFIGURED hostname, with verification left on. The behaviour over
    # real TLS lives in tests/test_proxy_check.py; this pins that the release
    # actually goes through it.
    assert "comrade.example.test" in calls[proxy], calls[proxy]
    assert "--resolve" in calls[proxy], calls[proxy]
    assert "--insecure" not in calls[proxy], calls[proxy]
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

def _double(path, name, extra=()):
    lines = ["#!/bin/sh", 'echo "%s $*" >> "$DEPLOY_LOG"' % name, *extra, "exit 0"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _fake_host(tmp_path, *, failure="", env_host=None, dotenv_host=None):
    """A host with the repository checked out, and every external faked."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (tmp_path / ".git").mkdir()
    # The committed helper, where a real checkout puts it.
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "proxy_check.sh",
                tmp_path / "scripts" / "proxy_check.sh")
    if dotenv_host:
        (tmp_path / ".env").write_text(
            "POSTGRES_PASSWORD=x\nCOMRADE_HOST=%s\n" % dotenv_host,
            encoding="utf-8", newline="\n")

    for name in ("git", "mkdir", "chown", "flock", "sleep"):
        _double(fake_bin / name, name)
    _double(fake_bin / "stat", "stat", ["echo 999"])
    _double(fake_bin / "docker", "docker", ["printf 'api\\nfrontend\\ncaddy\\n'"])
    _double(fake_bin / "curl", "curl",
            ['[ "$FAILURE" != proxydown ] || exit 7'])

    env = {
        **os.environ,
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "DEPLOY_LOG": (tmp_path / "commands.log").as_posix(),
        "FAILURE": failure,
    }
    env.pop("COMRADE_HOST", None)
    if env_host:
        env["COMRADE_HOST"] = env_host
    return env


def _as_the_workflow_does(tmp_path, env):
    """`git show <sha>:scripts/deploy_host.sh | sh -s <sha>` — the real shape.

    The script arrives on STDIN, so `$0` is `sh`. That is the only invocation
    in which the defect below exists; calling the file by path hides it.
    """
    script = (ROOT / "scripts" / "deploy_host.sh").read_text(encoding="utf-8")
    # encoding pinned: the script carries 🔴 markers, and piping it as text on
    # Windows encodes with cp1252, which cannot represent them. A test artifact,
    # not a product one — the deploy host reads UTF-8 either way.
    return subprocess.run(
        [SH, "-s", "abc123"], input=script, cwd=tmp_path, env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )


def test_the_release_runs_the_way_the_workflow_invokes_it(tmp_path):
    """🔴 THE DEFECT (fix.md F35, reopened). Piped into `sh -s`, `$0` is `sh`,
    so `$(dirname "$0")/proxy_check.sh` resolved to `./proxy_check.sh` while
    the committed helper is at `scripts/proxy_check.sh`. The check I added so a
    healthy deployment would stop being reported broken would itself have
    failed every healthy deployment."""
    env = _fake_host(tmp_path, dotenv_host="comrade.example.test")

    result = _as_the_workflow_does(tmp_path, env)

    assert result.returncode == 0, result.stdout + result.stderr
    calls = (tmp_path / "commands.log").read_text().splitlines()
    assert any("curl" in call and "comrade.example.test" in call
               for call in calls), calls


def test_the_hostname_can_come_from_the_deployments_env_file(tmp_path):
    """Compose interpolates `.env` for the containers; it exports nothing into
    the parent SSM shell. A host configured the documented way reached the new
    unset-host failure."""
    env = _fake_host(tmp_path, dotenv_host="from-dotenv.test")

    result = _as_the_workflow_does(tmp_path, env)

    assert result.returncode == 0, result.stdout + result.stderr
    calls = (tmp_path / "commands.log").read_text().splitlines()
    assert any("from-dotenv.test" in call for call in calls), calls


def test_an_exported_hostname_still_wins(tmp_path):
    """An operator running this by hand with the variable set should not have
    it silently replaced by whatever is in the file."""
    env = _fake_host(tmp_path, env_host="from-env.test",
                     dotenv_host="from-dotenv.test")

    _as_the_workflow_does(tmp_path, env)

    calls = (tmp_path / "commands.log").read_text().splitlines()
    assert any("from-env.test" in call for call in calls), calls
    assert not any("from-dotenv.test" in call for call in calls), calls


def test_a_host_configured_nowhere_is_refused(tmp_path):
    """Still fails closed: checking `localhost` against a named site is the
    original defect, so an unknown hostname stops the release rather than
    checking something meaningless."""
    env = _fake_host(tmp_path)

    result = _as_the_workflow_does(tmp_path, env)

    assert result.returncode != 0
    assert "COMRADE_HOST" in result.stderr


def test_a_missing_helper_is_reported_rather_than_skipped(tmp_path):
    """A checkout without the helper is a broken release, not a passed check."""
    env = _fake_host(tmp_path, dotenv_host="comrade.example.test")
    (tmp_path / "scripts" / "proxy_check.sh").unlink()

    result = _as_the_workflow_does(tmp_path, env)

    assert result.returncode != 0
    assert "proxy_check.sh" in result.stderr

