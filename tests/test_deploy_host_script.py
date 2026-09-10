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
    assert "git show $GITHUB_SHA:scripts/deploy_host.sh" in workflow
    assert "group: deploy-pilot" in workflow
    assert "cancel-in-progress: false" in workflow


def test_the_workflow_runs_the_release_from_a_file_not_a_pipe():
    """🔴 THE DEFECT (fix.md F58). The release used to arrive as
    `git show <sha>:scripts/deploy_host.sh | sh -s <sha>`, which makes the
    script its own stdin. `docker compose run` reads stdin, so a one-off
    container can swallow the REST OF THE SCRIPT: sh reaches end of input,
    exits 0, and the deploy reports success having never migrated, never
    activated and never checked readiness.

    Measured on the pilot host: a six-second deploy that stopped dead after
    step 4b's "Valid configuration" and left all four containers on the
    previous images, while the workflow went green.

    It is a RACE - how much sh has buffered when the compose run grabs the
    pipe - so it hit some releases and not others, and no single passing run
    demonstrates a fix. Running from a file removes the class by construction,
    which is what this pins.
    """
    workflow = (ROOT / ".github/workflows/deploy-pilot.yml").read_text()
    # Comments stripped, because the comment above the fix QUOTES the broken
    # form in order to explain it — and a blanket substring search over the
    # whole file finds that and fails on the explanation.
    code = "\n".join(line for line in workflow.splitlines()
                     if not line.lstrip().startswith("#"))

    assert "deploy_host.sh | sh" not in code, (
        "the release is piped into sh again; a compose run can eat the rest of it"
    )
    assert "sh /tmp/comrade-deploy.sh $GITHUB_SHA" in workflow
    # The exit code must survive the cleanup, or a failed deploy reports success.
    assert "rc=$?" in workflow and "exit $rc" in workflow


def test_every_one_off_container_in_the_release_closes_its_stdin():
    """The second half of the F58 fix, and the half that is testable anywhere.

    🔴 I could not write a test that REPRODUCES the truncation. It is a
    race between how much `sh` has buffered and when the one-off container
    grabs the pipe, and it does not reproduce in this harness: piped past a
    stdin-draining docker double on this machine, the release still reached
    migration and activation. A test that passes on the broken shape is worse
    than no test, so that attempt was deleted rather than kept as reassurance.
    It was demonstrated on the pilot host instead, and the ledger records it
    there.

    What IS checkable everywhere is the invariant: no command in the release
    may be left holding the script's stdin. `docker compose run` reads stdin,
    so every one of them redirects from /dev/null, and the workflow no longer
    hands the script to `sh` on stdin at all (the test above).
    """
    release = (ROOT / "scripts" / "deploy_host.sh").read_text(encoding="utf-8")
    code = "\n".join(line for line in release.splitlines()
                     if not line.lstrip().startswith("#"))
    # `run` invocations, joined across the backslash continuations they use.
    joined = code.replace("\\\n", " ")
    runs = [line.strip() for line in joined.splitlines()
            if "$COMPOSE run" in line]

    assert runs, "no one-off containers found; this test is guarding nothing"
    unguarded = [r for r in runs if "/dev/null" not in r]
    assert not unguarded, (
        "these one-off containers still inherit the release's stdin, so piped"
        f" into a shell they can swallow the rest of it: {unguarded}"
    )


# ---------------------------------------------------------------------------
# Hackathon preflight — the two things the live host was missing
# ---------------------------------------------------------------------------

def test_the_worker_group_comes_from_the_socket_not_a_default(tmp_path):
    """🔴 THE DEFECT (fix.md, hackathon preflight). Both workers join
    `${COMRADE_DOCKER_GID:-999}` and mount the daemon socket. 999 is a guess;
    on the pilot host the socket's group is 113, so the agent worker sat in a
    group that owns nothing and could not run a container at all.

    docker-compose.yml says it beside the setting — "the gid differs per host
    and a wrong one fails at runtime rather than at build" — and this is what
    makes it fail at deploy time instead. The double reports 4242, and that is
    what the release has to carry into Compose.
    """
    env = _fake_host(tmp_path, caddy_host="comrade.example.test")
    # Two different questions of the same command: the workspaces OWNER (%u)
    # and the socket's GROUP (%g). Answering both with one number is how the
    # first version of this test made the release refuse its own workspaces.
    _double(tmp_path / "bin" / "stat", "stat",
            ['case "$*" in', '  *"%g"*) echo 4242 ;;', '  *) echo 999 ;;',
             'esac'])

    result = _as_the_workflow_does(tmp_path, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "docker socket group: 4242" in result.stdout, result.stdout


def test_an_unreadable_socket_stops_the_release(tmp_path):
    """Refused rather than defaulted. A release that activates workers which
    cannot reach the daemon is the outage this exists to prevent, and it is
    invisible until someone asks for repository work."""
    env = _fake_host(tmp_path, caddy_host="comrade.example.test")
    _double(tmp_path / "bin" / "stat", "stat",
            ['[ "$*" = "-c %g /var/run/docker.sock" ] && exit 1', "echo 999"])

    result = _as_the_workflow_does(tmp_path, env)

    assert result.returncode != 0
    assert "docker.sock" in result.stderr, result.stderr
    calls = (tmp_path / "commands.log").read_text().splitlines()
    assert not any(" up " in call for call in calls), (
        "the stack was activated with workers that cannot reach the daemon"
    )


def test_the_sandbox_image_is_built_with_the_candidate(tmp_path):
    """🔴 THE DEFECT (fix.md, hackathon preflight). The sandbox image is not a
    Compose service, so `$COMPOSE build` never touched it and the host kept
    whatever `comrade-sandbox:latest` it already had — one with Python and no
    Node, while the candidate's Dockerfile adds both. Every JavaScript
    repository task failed on a release that reported success."""
    env = _fake_host(tmp_path, caddy_host="comrade.example.test")

    result = _as_the_workflow_does(tmp_path, env)

    assert result.returncode == 0, result.stdout + result.stderr
    calls = (tmp_path / "commands.log").read_text().splitlines()
    built = [c for c in calls
             if "build" in c and "docker/sandbox.Dockerfile" in c]
    assert built, f"the sandbox image was never built: {calls}"
    assert "comrade-sandbox:latest" in built[0], built[0]

    # Before activation, with the rest of the candidate: a broken sandbox
    # Dockerfile has to stop the deploy, not the first member who asks.
    order = {c: i for i, c in enumerate(calls)}
    up = next(i for c, i in order.items() if " up " in c)
    assert order[built[0]] < up, calls


def test_the_sandbox_image_matches_the_one_the_agent_looks_for():
    """One name. `agent/sandbox.py` tells the member to build
    `comrade-sandbox:latest` when it is missing, and `shared/config.py`
    defaults to it; a release that built a different tag would leave the same
    error message pointing at an image that now exists under another name."""
    from shared.config import settings

    release = (ROOT / "scripts" / "deploy_host.sh").read_text(encoding="utf-8")

    assert f"-t {settings.comrade_sandbox_image} ." in release, (
        "the release builds a tag the agent does not look for"
    )


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
    # 🔴 The HOSTNAME must not come from `.env`, which is the finding. Not "the
    # word .env never appears": the release also writes the resolved docker
    # socket group there, so Compose has it on a later `up -d`, and a blanket
    # ban made that legitimate write look like the defect. Scoped to the block
    # that decides the hostname, which is what F35 was about.
    start = code.index("caddy_host=$(")
    end = code.index('proxy_check.sh "$COMRADE_HOST"')
    assert ".env" not in code[start:end], (
        f"the hostname block reads .env again: {code[start:end]}"
    )
    # `config --services` asks a yes/no question about which services exist.
    # Any OTHER use of `config` means the resolved model is being read, and
    # every env_file service carries a COMRADE_HOST that is not caddy's.
    scrapes = [line.strip() for line in code.splitlines()
               if "$COMPOSE config" in line and "--services" not in line]
    assert not scrapes, (
        "the release is reading Compose's resolved model again; caddy's value"
        f" is not the first one in it: {scrapes}"
    )


def test_the_socket_group_is_persisted_for_later_compose_invocations(tmp_path):
    """🔴 THE DEFECT (found post-deployment). Exporting COMRADE_DOCKER_GID covers
    this script's own `up -d` and nothing else.

    `.env` is what Compose reads, so an operator running `docker compose up -d`
    or `compose run` afterwards — after a reboot, to restart one service, to look
    at something — gets the `:-999` default back and the workers silently lose
    the daemon. Measured on the pilot host: `compose run agent-worker` failed
    with "permission denied while trying to connect to the docker API" while the
    containers the deploy had created were fine.

    Rewritten from the socket every deploy, so `.env` is a cache of the socket
    rather than a second opinion about it.
    """
    env = _fake_host(tmp_path, caddy_host="comrade.example.test")
    (tmp_path / ".env").write_text("POSTGRES_PASSWORD=x\n",
                                   encoding="utf-8", newline="\n")
    _double(tmp_path / "bin" / "stat", "stat",
            ['case "$*" in', '  *"%g"*) echo 4242 ;;', '  *) echo 999 ;;', 'esac'])

    result = _as_the_workflow_does(tmp_path, env)

    assert result.returncode == 0, result.stdout + result.stderr
    written = (tmp_path / ".env").read_text()
    assert "COMRADE_DOCKER_GID=4242" in written, written


def test_a_stale_socket_group_in_the_env_is_replaced(tmp_path):
    """The socket is the source of truth. A value left in `.env` by an earlier
    deploy, on a host whose group has since changed, must not win."""
    env = _fake_host(tmp_path, caddy_host="comrade.example.test")
    (tmp_path / ".env").write_text("COMRADE_DOCKER_GID=999\nPOSTGRES_PASSWORD=x\n",
                                   encoding="utf-8", newline="\n")
    _double(tmp_path / "bin" / "stat", "stat",
            ['case "$*" in', '  *"%g"*) echo 4242 ;;', '  *) echo 999 ;;', 'esac'])

    result = _as_the_workflow_does(tmp_path, env)

    assert result.returncode == 0, result.stdout + result.stderr
    written = (tmp_path / ".env").read_text()
    assert "COMRADE_DOCKER_GID=4242" in written, written
    assert "COMRADE_DOCKER_GID=999" not in written, written


# ---------------------------------------------------------------------------
# F56 — the release migrates through an image it never builds
# ---------------------------------------------------------------------------


def _build_flags() -> list[str]:
    """The flags the release passes to its own `compose build`.

    Quotes removed the way the shell removes them: the script writes
    `--profile "*"` so the glob never reaches pathname expansion, and a test
    that forwarded the quote characters would be asking Compose for a profile
    literally named `"*"`.
    """
    script = (ROOT / "scripts" / "deploy_host.sh").read_text(encoding="utf-8")
    line = next(l.strip() for l in script.splitlines()
                if l.strip().startswith("$COMPOSE") and l.strip().endswith(" build"))
    return [word.strip("\"'") for word in line.split()[1:-1]]


def test_the_release_builds_the_services_it_is_going_to_run():
    """🔴 THE DEFECT (fix.md F56, found on the pilot host). `migrate` is
    profile-gated, and a bare `compose build` builds only the services in the
    DEFAULT profile — so the release never rebuilt it. Measured: `comrade-api`
    built 2026-09-10 11:15 for e2aae4e while `comrade-migrate` still said
    2026-09-09 21:27, two deploys behind.

    That image is not incidental. `deploy_host.sh` applies migrations with
    `$COMPOSE run --rm --no-deps -T migrate`, so a release adding a migration
    would run the OLD migrator, which does not contain the new file, and report
    success having applied nothing — new code against an unmigrated schema.

    Derived from the Compose file rather than naming `migrate`, so a second
    profile-gated service added later is covered by this test on the day it
    appears.
    """
    import yaml

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    gated = {name for name, spec in compose["services"].items()
             if spec.get("profiles") and spec.get("build")}
    assert gated, "no profile-gated buildable service; this test has nothing to guard"

    flags = _build_flags()

    assert "--profile" in flags, (
        f"the release builds with {flags or 'no flags'}, so the default profile"
        f" is all it builds — {sorted(gated)} keeps whatever image the host"
        " already had, including the one that applies migrations"
    )
    enabled = {flags[i + 1] for i, f in enumerate(flags) if f == "--profile"}
    assert "*" in enabled or gated <= enabled, (
        f"the release enables {sorted(enabled)} but has to build {sorted(gated)}"
    )


@needs_compose
def test_no_buildable_service_is_skipped_by_the_release_build(tmp_path):
    """The same invariant, asked of Compose itself rather than of an argv.

    `--profile` semantics are Compose's, not ours, and this runs the release's
    own flags through the committed files: every service with a build context
    has to appear in the service list that build sees.
    """
    project = _project(tmp_path, "COMRADE_HOST=comrade.example.test")
    # 🔴 EVERY profile, to enumerate. The first version of this test asked
    # `config` with no profile — which OMITS the gated services from the model
    # entirely — so `buildable` was the six default services, `visible` was the
    # same six, and it passed against the live defect. A test that cannot see
    # the thing it guards is not one.
    everything = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml",
         "-f", "docker-compose.prod.yml", "--profile", "*", "config"],
        cwd=project, capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    assert everything.returncode == 0, everything.stderr[-400:]
    import yaml

    model = yaml.safe_load(everything.stdout)
    buildable = {name for name, spec in model["services"].items() if spec.get("build")}
    assert "migrate" in buildable, sorted(buildable)

    listed = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml",
         "-f", "docker-compose.prod.yml", *_build_flags(), "config", "--services"],
        cwd=project, capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    assert listed.returncode == 0, listed.stderr[-400:]
    visible = set(listed.stdout.split())

    assert buildable <= visible, (
        f"{sorted(buildable - visible)} would never be rebuilt by the release"
    )
