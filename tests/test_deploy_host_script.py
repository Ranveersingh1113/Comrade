import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
SH = shutil.which("sh") or "C:/Program Files/Git/bin/sh.exe"


def _posix(path) -> str:
    """A Windows path in the form Git Bash searches.

    🔴 THE HARNESS DEFECT (fix.md, fourth review). These tests prepended their
    double directory to the WINDOWS environment PATH, separated by `;`. Git
    Bash builds its own POSIX PATH at startup, and whether a `;`-separated
    Windows entry survives that conversion depends on the host — on one machine
    the doubles won, on another `/mingw64/bin` came first and `git` resolved to
    the REAL binary while `flock` resolved to the double.

    Where it lost, the script ran a real `git fetch origin abc123`, failed with
    "couldn't find remote ref", and never reached the stage under test. Four
    parameterised failure cases still passed, because they asserted only a
    nonzero exit and the absence of activation — both of which an unrelated
    early failure also satisfies.

    So precedence is established INSIDE the launched shell, in its own path
    syntax, and asserted before the script runs.
    """
    text = str(Path(path))
    if len(text) > 1 and text[1] == ":":
        return "/" + text[0].lower() + text[2:].replace("\\", "/")
    return text.replace("\\", "/")


def _in_shell(fake_bin, command: str, *, env=None):
    """Run `command` with the doubles ahead of everything else."""
    prelude = f'export PATH="{_posix(fake_bin)}:$PATH"\n'
    return subprocess.run(
        [SH, "-c", prelude + command], env=env, capture_output=True,
        text=True, encoding="utf-8", timeout=180,
    )


def _assert_doubles_win(fake_bin, env, *names):
    """Prove the doubles are what the script will find, before running it.

    A test whose doubles are not in effect is a test of something else, and
    this says so out loud rather than letting an unrelated early failure look
    like the expected one.
    """
    for name in names:
        resolved = _in_shell(fake_bin, f"command -v {name}", env=env)
        assert resolved.returncode == 0, (name, resolved.stderr)
        first = resolved.stdout.strip().splitlines()[0]
        assert first.startswith(_posix(fake_bin)), (
            f"{name} resolves to {first!r}, not the double in"
            f" {_posix(fake_bin)} — the script would run the real one"
        )


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
  *"exec -T caddy printenv COMRADE_HOST")
    # 🔴 The site's own hostname, read from the container serving it.
    echo comrade.example.test; exit 0 ;;
  *config)
    # The resolved model, with an env_file service's copy FIRST — which is what
    # a key search finds, and is not the name Caddy serves. A release that goes
    # back to scraping this fails the assertions below instead of passing them.
    printf '      COMRADE_HOST: from-env-file.test\\n'; exit 0 ;;
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
    env = {**os.environ,
           "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
           "DEPLOY_LOG": log.as_posix(), "FAILURE": failure,
           }
    # NOT set here. The release reads the hostname out of the running caddy
    # container, which the double above answers, so an inherited variable
    # cannot be what makes this pass.
    env.pop("COMRADE_HOST", None)
    # 🔴 Asserted BEFORE the script runs, and the path put in front INSIDE the
    # shell. Prepending to the Windows PATH is not enough on every host (see
    # `_posix`), and where it lost the script ran a real `git fetch origin
    # abc123`, died there, and every failure case still "passed" on that
    # unrelated early exit.
    _assert_doubles_win(fake_bin, env, "git", "docker", "flock", "curl")
    result = subprocess.run(
        [SH, "-c", f'export PATH="{_posix(fake_bin)}:$PATH"\nexec sh "$0" "$@"',
         str(ROOT / "scripts/deploy_host.sh"), "abc123"],
        cwd=tmp_path, env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    calls = log.read_text().splitlines()
    # The fetch is the first thing the script does after taking the lock, so
    # its absence means the run never got past the lock — correct only for the
    # lock case itself.
    if failure != "lock":
        assert any("git fetch" in call for call in calls), (
            "the release never reached its own fetch, so whatever failed was"
            f" not the {failure or 'success'} case: {calls}"
        )
    # Each failure case names the call that PROVES the script reached that
    # stage. Without these, an unrelated early exit satisfies "nonzero and no
    # activation", which is all these used to check — and that is exactly what
    # happened on a host where the git double did not win.
    STAGE = {
        "build": lambda c: any(call.endswith(" build") for call in c),
        "migration": lambda c: any("-T migrate" in call for call in c),
        "proxyconfig": lambda c: any("validate --config" in call for call in c),
        "proxydown": lambda c: any(call.startswith("curl ") for call in c),
        "lock": lambda c: any(call.startswith("flock ") for call in c),
    }
    if failure:
        assert result.returncode != 0, calls
        assert STAGE[failure](calls), (
            f"the {failure} case exited without reaching its own stage: {calls}"
        )
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


def _fake_host(tmp_path, *, failure="", caddy_host=None, model_host=None,
               dotenv_host=None):
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
    # Three different questions. `config --services` says which services exist;
    # `exec -T caddy printenv` is the site's own hostname; `config` is the
    # resolved model, whose FIRST COMRADE_HOST belongs to an env_file service
    # and is deliberately a different value here.
    answers = ['case "$*" in',
               '  *"config --services") printf \'api\\nfrontend\\ncaddy\\n\' ;;']
    if caddy_host:
        answers.append(
            '  *"exec -T caddy printenv COMRADE_HOST") echo %s ;;' % caddy_host)
    if model_host:
        answers.append(
            "  *config) printf '      COMRADE_HOST: %s\\n' ;;" % model_host)
    answers.append('esac')
    _double(fake_bin / "docker", "docker", answers)
    _double(fake_bin / "curl", "curl",
            ['[ "$FAILURE" != proxydown ] || exit 7'])

    env = {
        **os.environ,
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "DEPLOY_LOG": (tmp_path / "commands.log").as_posix(),
        "FAILURE": failure,
    }
    # Compose exports nothing into the parent SSM shell, and preferring the
    # shell here would have the probe check a name the site is not serving.
    env.pop("COMRADE_HOST", None)
    return env


def _as_the_workflow_does(tmp_path, env):
    """`git show <sha>:scripts/deploy_host.sh | sh -s <sha>` — the real shape.

    The script arrives on STDIN, so `$0` is `sh`. That is the only invocation
    in which the defect below exists; calling the file by path hides it.
    """
    # The doubles are put in front INSIDE this shell and asserted first, so a
    # result here is a result about the script rather than about whichever
    # `git` the host happened to offer.
    fake_bin = tmp_path / "bin"
    _assert_doubles_win(fake_bin, env, "git", "docker", "curl", "flock")
    script = (ROOT / "scripts" / "deploy_host.sh").read_text(encoding="utf-8")
    # encoding pinned: the script carries 🔴 markers, and piping it as text on
    # Windows encodes with cp1252, which cannot represent them. A test artifact,
    # not a product one — the deploy host reads UTF-8 either way.
    return subprocess.run(
        [SH, "-c", f'export PATH="{_posix(fake_bin)}:$PATH"\nexec sh -s "$@"',
         "sh", "abc123"],
        input=script, cwd=tmp_path, env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=180,
    )


def test_the_release_runs_the_way_the_workflow_invokes_it(tmp_path):
    """🔴 THE DEFECT (fix.md F35, reopened). Piped into `sh -s`, `$0` is `sh`,
    so `$(dirname "$0")/proxy_check.sh` resolved to `./proxy_check.sh` while
    the committed helper is at `scripts/proxy_check.sh`. The check I added so a
    healthy deployment would stop being reported broken would itself have
    failed every healthy deployment."""
    env = _fake_host(tmp_path, caddy_host="comrade.example.test")

    result = _as_the_workflow_does(tmp_path, env)

    assert result.returncode == 0, result.stdout + result.stderr
    calls = (tmp_path / "commands.log").read_text().splitlines()
    assert any("curl" in call and "comrade.example.test" in call
               for call in calls), calls


def test_the_hostname_comes_from_the_deployments_own_configuration(tmp_path):
    """🔴 THE DEFECT this began as: Compose interpolates `.env` for the
    containers and exports nothing into the parent SSM shell, so a host
    configured the documented way reached the unset-host failure and a
    healthy release was reported broken.

    Nothing is in the environment here — `_fake_host` removes it — and the
    release still has to arrive at the configured name."""
    env = _fake_host(tmp_path, caddy_host="from-compose.test")

    result = _as_the_workflow_does(tmp_path, env)

    assert result.returncode == 0, result.stdout + result.stderr
    calls = (tmp_path / "commands.log").read_text().splitlines()
    assert any("from-compose.test" in call for call in calls), calls


def test_the_probe_asks_for_the_name_caddy_is_serving(tmp_path):
    """🔴 THE FIFTH-REVIEW DEFECT, through the complete stdin invocation.

    Three hostnames are in play and only one of them is the site: `.env` holds
    one (which the release must not parse), the resolved model lists an
    env_file service's copy FIRST (which a key search finds), and the caddy
    container has the one Caddy actually serves.

    The probe has to ask for the third. The other two are exactly what the two
    previous attempts at F35 reached instead.
    """
    env = _fake_host(tmp_path, dotenv_host="only-in-the-file.test",
                     model_host="first-in-the-model.test",
                     caddy_host="what-caddy-serves.test")

    result = _as_the_workflow_does(tmp_path, env)

    assert result.returncode == 0, result.stdout + result.stderr
    calls = (tmp_path / "commands.log").read_text().splitlines()
    probe = [call for call in calls if call.startswith("curl ")]
    assert probe, calls
    assert all("what-caddy-serves.test" in call for call in probe), probe
    # The two wrong answers, each one a previous version of this fix.
    assert not any("only-in-the-file.test" in call for call in calls), calls
    assert not any("first-in-the-model.test" in call for call in calls), calls


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
    env = _fake_host(tmp_path, caddy_host="comrade.example.test")
    (tmp_path / "scripts" / "proxy_check.sh").unlink()

    result = _as_the_workflow_does(tmp_path, env)

    assert result.returncode != 0
    assert "proxy_check.sh" in result.stderr

def test_the_harness_refuses_to_run_when_a_double_is_not_in_effect(tmp_path):
    """🔴 The guard that makes every test in this file mean something.

    The defect it exists for is host-dependent: where Git Bash's PATH
    conversion drops the doubles' directory, `git` resolves to the real binary,
    the script fetches `abc123` for real, dies there, and every failure case
    below still satisfies "nonzero exit and no activation". On this machine the
    conversion happens to work, so the failure cannot be reproduced here — which
    is exactly why the assertion has to exist rather than be assumed.

    This proves the assertion has teeth by removing a double outright: whatever
    the host does with PATH, a missing double must stop the test rather than
    hand it the real command.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _double(fake_bin / "docker", "docker")
    env = {**os.environ, "DEPLOY_LOG": (tmp_path / "log").as_posix()}

    # The double that IS there passes.
    _assert_doubles_win(fake_bin, env, "docker")

    # The one that is not must fail loudly, naming what it found instead.
    with pytest.raises(AssertionError) as refused:
        _assert_doubles_win(fake_bin, env, "git")

    assert "git" in str(refused.value)


def test_the_double_directory_is_searched_in_the_shells_own_syntax(tmp_path):
    r"""`_posix` is why the prelude works: Git Bash searches POSIX paths, and a
    `C:\...` entry separated by `;` is not one."""
    assert _posix(tmp_path).startswith("/")
    assert "\\" not in _posix(tmp_path)
    assert _posix("/already/posix") == "/already/posix"


# ---------------------------------------------------------------------------
# F35 — the hostname is CADDY'S, and Caddy is the only service that has it
# ---------------------------------------------------------------------------


def _compose_available() -> bool:
    try:
        return subprocess.run(["docker", "compose", "version"],
                              capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


needs_compose = pytest.mark.skipif(
    not _compose_available(),
    reason="docker compose is what resolves the hostname; nothing to compare to",
)

#: The value shapes the finding names. Each is written into `.env` verbatim.
HOSTNAME_SHAPES = {
    "plain": ("COMRADE_HOST=comrade.example.test", "comrade.example.test"),
    "commented": ("COMRADE_HOST=comrade.example.test # public hostname",
                  "comrade.example.test"),
    "quoted": ('COMRADE_HOST="comrade.example.test"', "comrade.example.test"),
    "single-quoted": ("COMRADE_HOST='comrade.example.test'",
                      "comrade.example.test"),
    "trailing-space": ("COMRADE_HOST=comrade.example.test   ",
                       "comrade.example.test"),
    "interpolated": ("COMRADE_BASE=example.test\nCOMRADE_HOST=comrade.${COMRADE_BASE}",
                     "comrade.example.test"),
}


def _project(tmp_path, dotenv_body):
    """The committed Compose files, with a `.env` of our choosing beside them."""
    project = tmp_path / "deploy"
    project.mkdir()
    for name in ("docker-compose.yml", "docker-compose.prod.yml"):
        shutil.copy(ROOT / name, project / name)
    (project / ".env").write_text(
        "POSTGRES_PASSWORD=secret\nCOMRADE_PREVIEW_DOMAIN=p.test\n"
        + dotenv_body.replace("\\n", "\n") + "\n",
        encoding="utf-8", newline="\n")
    return project


def _resolved(project, env=None):
    """Compose's resolved model, parsed as YAML rather than scraped."""
    import yaml

    out = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml",
         "-f", "docker-compose.prod.yml", "config"],
        cwd=project, env={**os.environ, **(env or {})},
        capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    assert out.returncode == 0, out.stderr[-400:]
    return yaml.safe_load(out.stdout), out.stdout


@needs_compose
def test_only_caddy_carries_the_interpolated_hostname(tmp_path):
    """🔴 THE DEFECT (fix.md, fifth review). Every service with
    `env_file: [.env]` also carries COMRADE_HOST — holding the LITERAL file
    value, which no shell variable can affect. Only caddy is given the
    interpolated `${COMRADE_HOST}`, and caddy is the one serving the site.

    Services are emitted alphabetically, so a search for the first COMRADE_HOST
    key finds agent-worker's. The release did exactly that, and the probe then
    asked for a name Caddy has no site for — the original F35 defect, put back
    by its own fix.

    It also produced the ledger's claim that Compose reverses interpolation
    precedence. It does not: that was the api's `env_file` copy being read.
    """
    project = _project(tmp_path, "COMRADE_HOST=from-dotenv.test")

    model, raw = _resolved(project, {"COMRADE_HOST": "from-shell.test"})

    services = model["services"]
    assert services["caddy"]["environment"]["COMRADE_HOST"] == "from-shell.test"
    # The shell does NOT reach the env_file services, because `env_file` copies
    # the file's literal text rather than interpolating it.
    for name in ("api", "agent-worker", "pipeline-worker"):
        assert services[name]["environment"]["COMRADE_HOST"] == "from-dotenv.test"

    # And the first one printed is not caddy's, which is what made a key search
    # the wrong tool no matter how carefully the key was matched.
    first = next(line.split(":", 1)[1].strip()
                 for line in raw.splitlines()
                 if line.strip().startswith("COMRADE_HOST:"))
    assert first == "from-dotenv.test", (
        "the first COMRADE_HOST is caddy's on this Compose version, so this"
        " test no longer reproduces the finding — check the release still asks"
        " caddy rather than relying on the order"
    )


@needs_compose
@pytest.mark.parametrize("shape", sorted(HOSTNAME_SHAPES))
def test_compose_resolves_every_hostname_shape_for_caddy(tmp_path, shape):
    """The shapes the earlier rounds got wrong, kept: inline comment, both
    quotings, trailing whitespace, `${VAR}` interpolation.

    They are Compose's problem now rather than the release's — which is the
    point of not carrying a second parser — so what is pinned here is that
    CADDY's resolved value is the bare hostname for each of them.
    """
    body, expected = HOSTNAME_SHAPES[shape]

    model, _ = _resolved(_project(tmp_path, body))

    assert model["services"]["caddy"]["environment"]["COMRADE_HOST"] == expected


@needs_compose
def test_the_running_caddy_container_reports_the_resolved_hostname(tmp_path):
    """The release reads `printenv COMRADE_HOST` from the caddy container,
    because docker/Caddyfile's site address is `{$COMRADE_HOST}` and Caddy
    substitutes it from the container environment when it loads its config.

    This runs the real image with the value Compose resolved, so the assumption
    that reading it back is possible at all is checked against the image rather
    than against a double.
    """
    project = _project(tmp_path, "COMRADE_HOST=from-dotenv.test")
    model, _ = _resolved(project, {"COMRADE_HOST": "from-shell.test"})
    resolved = model["services"]["caddy"]["environment"]["COMRADE_HOST"]
    image = model["services"]["caddy"]["image"]

    out = subprocess.run(
        ["docker", "run", "--rm", "-e", f"COMRADE_HOST={resolved}", image,
         "printenv", "COMRADE_HOST"],
        capture_output=True, text=True, encoding="utf-8", timeout=300,
    )

    assert out.returncode == 0, out.stderr[-400:]
    assert out.stdout.strip() == "from-shell.test"


def test_the_release_reads_caddys_environment_and_parses_nothing():
    """One source, and it is the container that serves the site. A second
    parser beside Compose's produced a hostname Compose never configured; a
    key search across Compose's own output produced another service's."""
    script = (ROOT / "scripts" / "deploy_host.sh").read_text(encoding="utf-8")
    code = "\n".join(line for line in script.splitlines()
                     if not line.lstrip().startswith("#"))

    assert "exec -T caddy printenv COMRADE_HOST" in code
    assert ".env" not in code, "the release reads .env itself again"
    # `config --services` asks a yes/no question about which services exist.
    # Any OTHER use of `config` means the resolved model is being read, and
    # every env_file service carries a COMRADE_HOST that is not caddy's.
    scrapes = [line.strip() for line in code.splitlines()
               if "$COMPOSE config" in line and "--services" not in line]
    assert not scrapes, (
        "the release is reading Compose's resolved model again; caddy's value"
        f" is not the first one in it: {scrapes}"
    )
