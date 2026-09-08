"""What the dependency-install phase does with a hook that will not shut up.

🔴 THE DEFECT (fix.md F05). `run_setup` was the last `capture_output=True`.

Every other path that runs a repository's code goes through `run_bounded`,
which drains both pipes into a fixed byte budget while the process runs. The
setup phase did not: it called `subprocess.run(capture_output=True)` and
clipped the result afterwards with `_clip`.

Clipping afterwards protects the LOG. It does not protect the worker, which
has already held the whole thing in memory to clip it — and this is the one
phase with the network on, running `pip install`'s setup.py and npm's
postinstall scripts, for up to ten minutes. A hook that prints in a loop grows
the pipeline worker until it dies, and it takes every other team's job with it.

So these tests measure PEAK memory, not the returned string. The returned
string was always short; that was exactly the thing that made the defect
invisible.
"""
import subprocess
import sys
import tracemalloc
from pathlib import Path

import pytest

from agent import sandbox
from agent.sandbox import MAX_OUTPUT_BYTES, SandboxError, run_setup

#: Comfortably more than the cap, and enough that holding it all is visible
#: against the noise of a test process. Per stream.
FLOOD_BYTES = 8 * 1024 * 1024


def _noisy(stdout_bytes: int, stderr_bytes: int, code: int = 0) -> list[str]:
    """A real child process that floods both streams and then exits.

    A real one, not a fake result object: the whole question is what happens
    to bytes travelling through a pipe, and a fake hands back an answer
    instead of producing the situation.
    """
    script = (
        "import sys\n"
        "chunk = b'x' * 65536\n"
        f"for _ in range({stdout_bytes} // 65536):\n"
        "    sys.stdout.buffer.write(chunk)\n"
        f"for _ in range({stderr_bytes} // 65536):\n"
        "    sys.stderr.buffer.write(chunk)\n"
        "sys.stdout.flush(); sys.stderr.flush()\n"
        f"sys.exit({code})\n"
    )
    return [sys.executable, "-c", script]


@pytest.fixture
def setup_host(tmp_path, monkeypatch):
    """A configured setup phase whose `docker run` is a local process.

    Docker itself is not the subject here. What is under test is the code
    between the argv and the result — which pipes it opens, and how much of
    what comes back it keeps.
    """
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "requirements.txt").write_text("cowsay==6.1\n", encoding="utf-8")

    monkeypatch.setattr(
        "shared.config.settings.comrade_setup_proxy_url", "http://proxy:3128")
    monkeypatch.setattr(
        "shared.config.settings.comrade_setup_proxy_container", "comrade-registry-proxy")

    networks: list[str] = []
    dropped: list[str] = []
    monkeypatch.setattr(sandbox, "_internal_network",
                        lambda name, attach: (networks.append(name), name)[1])
    monkeypatch.setattr(sandbox, "_drop_network",
                        lambda name, attached: dropped.append(name))
    monkeypatch.setattr(sandbox, "_kill", lambda name: None)

    substituted: list[list[str]] = []

    class _Subprocess:
        """`subprocess`, with the docker argv swapped for the noisy child.

        Substituted at the module the sandbox imports rather than at one
        function, so the test cannot accidentally pin WHICH call is used —
        which is the very thing the fix changes.
        """
        PIPE = subprocess.PIPE
        TimeoutExpired = subprocess.TimeoutExpired
        CalledProcessError = subprocess.CalledProcessError
        DEVNULL = subprocess.DEVNULL

        def __init__(self) -> None:
            self.argv = _noisy(FLOOD_BYTES, FLOOD_BYTES)

        def Popen(self, argv, **kwargs):  # noqa: N802 - mirrors subprocess
            substituted.append(argv)
            return subprocess.Popen(self.argv, **kwargs)

        def run(self, argv, **kwargs):
            substituted.append(argv)
            kwargs.pop("errors", None) if not kwargs.get("text") else None
            return subprocess.run(self.argv, **kwargs)

    fake = _Subprocess()
    monkeypatch.setattr(sandbox, "subprocess", fake)

    return {"root": root, "fake": fake, "networks": networks,
            "dropped": dropped, "docker_argv": substituted}


def _peak_of(call) -> tuple[object, int]:
    """Run `call`, returning its result and the peak Python allocation."""
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        result = call()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, peak


# ---------------------------------------------------------------------------

def test_a_flooding_install_hook_does_not_grow_the_worker(setup_host):
    """🔴 The finding's own case. 16 MB out of a hook, on a phase that runs
    for up to ten minutes with the network on."""
    result, peak = _peak_of(lambda: run_setup(
        ["sh", "-c", "pip install -r requirements.txt"],
        root=setup_host["root"], deps="comrade-deps-probe",
    ))

    # A generous ceiling: the budget is two streams of MAX_OUTPUT_BYTES, and
    # decoding and joining them costs a few multiples more. 16 MB held at once
    # clears it by a mile, which is the point — this is not a tight
    # measurement, it is the difference between bounded and not.
    assert peak < 8 * MAX_OUTPUT_BYTES, (
        f"held {peak:,} bytes of a {2 * FLOOD_BYTES:,}-byte flood"
    )
    assert result["exit_code"] == 0


def test_the_flood_is_reported_rather_than_silently_dropped(setup_host):
    """A shortened install log that does not say it was shortened is how "the
    build passed" gets read off a log whose error fell out of the middle."""
    result = run_setup(
        ["sh", "-c", "pip install -r requirements.txt"],
        root=setup_host["root"], deps="comrade-deps-probe",
    )

    assert result["output_limited"] is True
    # Two shortenings apply, and either notice is honest: the byte budget says
    # bytes were omitted, and the job-row clip says characters were dropped
    # from the middle. What must never happen is a quietly shortened log.
    assert "omitted" in result["stdout"] or "dropped from the middle" in result["stdout"]


def test_both_streams_are_drained_so_neither_can_block_the_other(setup_host):
    """A process writing hard to stderr while nobody reads it fills the pipe
    and stops. Reading one stream to exhaustion and then the other deadlocks
    against any hook noisy on the wrong one — and this phase's ten-minute
    timeout is how long that deadlock would last."""
    setup_host["fake"].argv = _noisy(FLOOD_BYTES, FLOOD_BYTES)

    result = run_setup(
        ["sh", "-c", "npm install"],
        root=setup_host["root"], deps="comrade-deps-probe",
    )

    assert result["timed_out"] is False
    assert result["stdout"] and result["stderr"]


def test_a_failed_install_still_reports_its_exit_code(setup_host):
    """The bound must not cost the result. A nonzero exit is how the pipeline
    tells a team its lockfile does not resolve."""
    setup_host["fake"].argv = _noisy(FLOOD_BYTES, 4096, code=1)

    result = run_setup(
        ["sh", "-c", "pip install -r requirements.txt"],
        root=setup_host["root"], deps="comrade-deps-probe",
    )

    assert result["exit_code"] == 1
    assert result["timed_out"] is False


def test_a_hook_that_never_finishes_still_times_out(setup_host):
    """The timeout is the outer bound on a phase that reaches the network."""
    setup_host["fake"].argv = [
        sys.executable, "-c", "import time; time.sleep(30)",
    ]

    result = run_setup(
        ["sh", "-c", "pip install -r requirements.txt"],
        root=setup_host["root"], deps="comrade-deps-probe", timeout=2,
    )

    assert result["timed_out"] is True
    assert result["exit_code"] is None


def test_the_per_run_network_is_reclaimed_however_it_ends(setup_host):
    """Left behind they accumulate until Docker runs out of address space,
    which surfaces as unrelated containers failing to start."""
    setup_host["fake"].argv = _noisy(FLOOD_BYTES, FLOOD_BYTES, code=1)

    run_setup(["sh", "-c", "pip install -r requirements.txt"],
              root=setup_host["root"], deps="comrade-deps-probe")

    assert setup_host["dropped"] == setup_host["networks"]
    assert len(setup_host["dropped"]) == 1


def test_a_missing_docker_is_still_a_sandbox_error(setup_host, monkeypatch):
    """Not a traceback out of subprocess. The pipeline turns this into a
    status a person reads."""
    def _no_docker(argv, **kwargs):
        raise FileNotFoundError(2, "docker")

    monkeypatch.setattr(setup_host["fake"], "Popen", _no_docker, raising=False)
    monkeypatch.setattr(setup_host["fake"], "run", _no_docker, raising=False)

    with pytest.raises(SandboxError):
        run_setup(["sh", "-c", "pip install -r requirements.txt"],
                  root=setup_host["root"], deps="comrade-deps-probe")

    # And the network still goes, because it was created before the failure.
    assert setup_host["dropped"] == setup_host["networks"]


def test_the_setup_argv_is_still_a_docker_run(setup_host):
    """The substitution above must not be able to hide a change of shape: the
    thing being bounded is still `docker run` on the sandbox image."""
    run_setup(["sh", "-c", "pip install -r requirements.txt"],
              root=setup_host["root"], deps="comrade-deps-probe")

    argv = setup_host["docker_argv"][0]
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--read-only" in argv
    assert Path(str(setup_host["root"])).as_posix() in " ".join(argv).replace("\\", "/")
