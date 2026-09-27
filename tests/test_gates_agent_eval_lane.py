"""fix.md F63 — `--with-agent-eval` could not reach or clean up its own lane.

🔴 THE DEFECT. The lane REFUSED to run unless an API was already answering on
:8000, and no invocation of `scripts/gates.sh` can leave one there:

* the backend precondition at the top exits 1 while anything answers on :8000,
  so one cannot have been started beforehand;
* `frontend/playwright.config.ts` owns the API the browser lane uses and stops
  it when that lane ends (fix.md F57);
* `--quick --with-agent-eval` skips the browser lane altogether.

🔴 AND THE HALF THE FIRST REPAIR GOT WRONG. Starting an API and a pipeline
worker is not enough: `POST /agent/turn` only ENQUEUES, and `agent.worker` is
what executes the queue. A lane without one admits every turn in the scenario
and runs none of them — and the first version of this file could not see that,
because its scenario double asked only whether an API marker existed.

Nothing real runs: `uv`, the interpreter it resolves, `npm`, `curl`, `docker`
and `powershell.exe` are doubles on PATH. What is under test is the script's
own lifecycle bookkeeping, and that is entirely in the script.
"""
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest


ROOT = Path(__file__).parents[1]
SH = shutil.which("sh") or "C:/Program Files/Git/bin/sh.exe"

# 🔴 The doubles are PROCESSES, and the test asserts they are gone. The first
# version of this file used marker files that each double removed on its own
# way out, which proves the double tidies up rather than that the script does.
#
# Each background job records its own pid; "the API answers on :8000" is then
# `kill -0` on that pid, which is the real relationship between a live server
# and an open port.
INTERPRETER = '''#!/bin/sh
echo "python $*" >> "$GATE_LOG"
# Releases the inherited pipes first. A survivor holding the gate's stdout
# makes the harness block on EOF, which is a timeout rather than a
# readable assertion about the process still being alive.
idle() { exec >/dev/null 2>&1; i=0; while [ "$i" -lt 600 ]; do sleep 1; i=$((i + 1)); done; }
case "$*" in
  "-m uvicorn"*)
      if [ -n "${ORPHAN:-}" ]; then
        # What `uv run` used to leave behind: the pid the script holds is a
        # PARENT, and the thing actually holding the port outlives its kill.
        sh -c 'i=0; while [ "$i" -lt 600 ]; do sleep 1; i=$((i + 1)); done' &
        echo $! > "$API_PID"
      else
        echo $$ > "$API_PID"
      fi
      idle ;;
  "-m pipeline.worker"*)
      echo $$ > "$PIPELINE_PID"
      # 🔴 Both real workers DRAIN on their first TERM: they finish the job in
      # hand and only then exit. A double that dies instantly cannot show what
      # the lane does about one that does not.
      [ "${STUBBORN:-}" = pipeline ] && trap '' TERM
      idle ;;
  "-m agent.worker"*)    echo $$ > "$AGENT_PID"; idle ;;
esac
exit 0
'''

UV = '''#!/bin/sh
echo "uv $*" >> "$GATE_LOG"
case "$*" in
  "run python -c"*)
      # The interpreter the lane starts its servers with, so the pid it holds
      # is the pid that serves.
      printf '%s\\n' "$FAKE_PY" ;;
  *sim.scenario*)
      # 🔴 The scenario needs ALL THREE. An agent turn is admitted by the API
      # and executed by the agent worker; the pipeline worker clones the repo.
      # A double that checks only the API passes a lane that can never answer.
      for who in "$API_PID" "$PIPELINE_PID" "$AGENT_PID"; do
        if [ ! -f "$who" ] || ! kill -0 "$(cat "$who")" 2>/dev/null; then
          echo "sim.scenario ran with no live $who" >&2; exit 4
        fi
      done
      # Long enough for a test to interrupt the gate in the middle of it.
      [ -z "${SCENARIO_SLEEP:-}" ] || sleep "$SCENARIO_SLEEP"
      exit "${SCENARIO_EXIT:-0}" ;;
esac
exit 0
'''

CURL = '''#!/bin/sh
echo "curl $*" >> "$GATE_LOG"
up() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }
case "$*" in
  *localhost:8000/ready*)
      if up "$API_PID" && up "$PIPELINE_PID" && up "$AGENT_PID"; then
        echo '{"status":"ready","checks":{"database":"ok","workers":"ok"}}'
      else
        echo '{"status":"not_ready","checks":{"workers":"none seen"}}'
      fi ;;
  *localhost:8000/health*)
      up "$API_PID" || exit 7
      echo '{"status":"ok","database":"ok"}' ;;
esac
exit 0
'''

OTHERS = {
    "npm": '#!/bin/sh\necho "npm $*" >> "$GATE_LOG"\nexit 0\n',
    # Every container the precondition asks after is running.
    "docker": '#!/bin/sh\necho "docker $*" >> "$GATE_LOG"\necho running\nexit 0\n',
    # The Windows-only check for a stray pipeline.worker: none running.
    "powershell.exe": "#!/bin/sh\nexit 0\n",
}


def _posix(path) -> str:
    text = str(Path(path))
    if len(text) > 1 and text[1] == ":":
        return "/" + text[0].lower() + text[2:].replace("\\", "/")
    return text.replace("\\", "/")


def _alive(pid: str) -> bool:
    """Asked of the same runtime that started it.

    These are MSYS pids on Windows and are meaningless to the Windows process
    table, so the question goes back through the same shell.
    """
    return subprocess.run(
        [SH, "-c", f'kill -0 {pid} 2>/dev/null'], timeout=30,
    ).returncode == 0


def _kill(pid: str) -> None:
    subprocess.run([SH, "-c", f'kill {pid} 2>/dev/null'], timeout=30)


class Lane:
    """One prepared invocation of the gate, and the pids it recorded."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir(exist_ok=True)
        self.fake_py = fake_bin / "fakepython"
        for path, body in (
            (self.fake_py, INTERPRETER),
            (fake_bin / "uv", UV),
            (fake_bin / "curl", CURL),
            *[(fake_bin / name, body) for name, body in OTHERS.items()],
        ):
            path.write_text(body, encoding="utf-8", newline="\n")
            path.chmod(0o755)
        self.fake_bin = fake_bin
        self.log = tmp_path / "gates.log"
        self.pid_files = {
            "API_PID": tmp_path / "api.pid",
            "PIPELINE_PID": tmp_path / "pipeline.pid",
            "AGENT_PID": tmp_path / "agent.pid",
        }

    def env_for(self, *, orphan=False, scenario_exit=0, stubborn="",
                scenario_sleep="", drain_seconds="30"):
        env = {
            **os.environ,
            "GATE_LOG": self.log.as_posix(),
            "FAKE_PY": _posix(self.fake_py),
            "SCENARIO_EXIT": str(scenario_exit),
            "STUBBORN": stubborn,
            "SCENARIO_SLEEP": scenario_sleep,
            "LANE_DRAIN_SECONDS": drain_seconds,
            **{name: path.as_posix() for name, path in self.pid_files.items()},
        }
        if orphan:
            env["ORPHAN"] = "1"
        else:
            env.pop("ORPHAN", None)
        return env

    def run(self, *args, orphan=False, scenario_exit=0, stubborn="",
            drain_seconds="30", timeout=300):
        env = self.env_for(orphan=orphan, scenario_exit=scenario_exit,
                           stubborn=stubborn, drain_seconds=drain_seconds)
        for path in self.pid_files.values():
            path.unlink(missing_ok=True)
        result = subprocess.run(
            [SH, "-c",
             f'export PATH="{_posix(self.fake_bin)}:$PATH"\nexec sh "$0" "$@"',
             str(ROOT / "scripts" / "gates.sh"), *args],
            env=env, capture_output=True, text=True, encoding="utf-8",
            timeout=timeout,
        )
        calls = (self.log.read_text(encoding="utf-8").splitlines()
                 if self.log.exists() else [])
        return result, calls

    def pids(self):
        return {name: path.read_text().strip()
                for name, path in self.pid_files.items() if path.exists()}

    def kill_everything(self):
        for pid in self.pids().values():
            _kill(pid)


@pytest.fixture
def lane(tmp_path):
    prepared = Lane(tmp_path)
    yield prepared
    # A test that fails half way must not leave a sleeper behind for the next.
    prepared.kill_everything()


def test_the_opted_in_scenario_is_reached_and_completes(lane):
    """One invocation, start to finish, with nothing answering on :8000 when it
    begins — which is the only state the script's own precondition allows."""
    result, calls = lane.run("--quick", "--with-agent-eval")

    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert "all gates passed" in result.stdout, result.stdout[-2000:]
    assert any("sim.scenario --check" in c for c in calls), calls


def test_the_lane_starts_the_agent_worker_that_executes_the_turns(lane):
    """🔴 The half the first repair missed. `POST /agent/turn` enqueues and
    `agent.worker` executes; a lane with only an API and a pipeline worker
    admits every turn in the scenario and answers none of them."""
    _, calls = lane.run("--quick", "--with-agent-eval")

    started = [c for c in calls if c.startswith("python -m ")]
    assert any("-m uvicorn" in c for c in started), started
    assert any("-m agent.worker" in c for c in started), started
    assert any("-m pipeline.worker" in c for c in started), started
    # All of them before the scenario, or the scenario had nobody to answer it.
    scenario = next(i for i, c in enumerate(calls) if "sim.scenario" in c)
    for name in ("uvicorn", "agent.worker", "pipeline.worker"):
        assert next(i for i, c in enumerate(calls) if name in c) < scenario, calls


def test_the_lane_waits_for_all_three_before_the_scenario(lane):
    """Started is not ready. The wait is `/ready`'s own worker check, which
    reports ok only once BOTH workers have beaten and the API can reach the
    database — so one poll covers all three processes."""
    _, calls = lane.run("--quick", "--with-agent-eval")

    readiness = [i for i, c in enumerate(calls) if "/ready" in c]
    assert readiness, calls
    scenario = next(i for i, c in enumerate(calls) if "sim.scenario" in c)
    assert readiness[0] < scenario, calls


def test_every_process_the_lane_started_is_stopped(lane):
    """🔴 The pids, not a marker a double removed for itself. A worker left
    behind drains the job queue other gates' tests enqueue, and an API left
    behind is the second writer the backend precondition refuses to run
    beside — so the next invocation fails at a check pointing nowhere near
    this lane."""
    result, _ = lane.run("--quick", "--with-agent-eval")

    assert result.returncode == 0, result.stdout[-2000:]
    recorded = lane.pids()
    assert set(recorded) == {"API_PID", "PIPELINE_PID", "AGENT_PID"}, recorded
    still = {name: pid for name, pid in recorded.items() if _alive(pid)}
    assert not still, f"the lane left these running: {still}"


def test_a_server_that_outlives_its_kill_fails_the_gate(lane):
    """🔴 It used to print a WARNING and let `all gates passed` follow it.

    A warning under a green line is a warning nobody reads, and the cost lands
    on the NEXT run as a refusal at the backend precondition. Modelled the way
    it actually happened: the pid the script holds is a parent, and the thing
    holding the port survives it.
    """
    result, _ = lane.run("--quick", "--with-agent-eval", orphan=True)

    assert result.returncode != 0, result.stdout[-2000:]
    assert "all gates passed" not in result.stdout, result.stdout[-2000:]
    assert "still answering on :8000" in result.stderr, result.stderr[-2000:]


def test_a_failing_scenario_stays_nonzero_and_still_cleans_up(lane):
    """Both halves. The exit status has to survive the cleanup, and the
    cleanup has to happen on the failing path — which is the path where
    leaving three processes behind is easiest and least noticed."""
    result, calls = lane.run("--quick", "--with-agent-eval", scenario_exit=3)

    assert result.returncode != 0, result.stdout[-2000:]
    assert "all gates passed" not in result.stdout
    assert any("sim.scenario" in c for c in calls), calls
    still = {name: pid for name, pid in lane.pids().items() if _alive(pid)}
    assert not still, f"the failing path left these running: {still}"


def test_a_second_invocation_needs_no_manual_cleanup(lane):
    """The property all of the above exists for: run it, run it again."""
    first, _ = lane.run("--quick", "--with-agent-eval")
    assert first.returncode == 0, first.stdout[-2000:]

    second, calls = lane.run("--quick", "--with-agent-eval")

    assert second.returncode == 0, second.stdout[-2000:] + second.stderr[-2000:]
    assert any("sim.scenario --check" in c for c in calls), calls


def test_nothing_is_started_without_the_flag(lane):
    """The control. Without it, a script that fell over before the lane would
    satisfy the assertions above by never getting there either."""
    result, calls = lane.run("--quick")

    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert not any("sim.scenario" in c for c in calls), calls
    assert not any(c.startswith("python -m ") for c in calls), calls


def test_a_worker_that_survives_the_first_signal_is_stopped(lane):
    """🔴 THE DEFECT (fix.md F63, third pass). `lane_stop` polled for 30s, then
    CLEARED the pid list, echoed the survivors and returned ZERO.

    That is not a hypothetical. Both real workers drain on their first TERM —
    they finish the job in hand and only then exit — and model or compiler work
    can outlast the window. So a pipeline or agent worker that was still going
    kept writing to the database while the gate printed `all gates passed`, and
    clearing the list meant the EXIT trap never tried again. The port check
    could not see it either: a worker holds no port.

    Here the API stops normally and the pipeline worker ignores TERM, which is
    the shape the review reproduced.
    """
    result, _ = lane.run("--quick", "--with-agent-eval",
                         stubborn="pipeline", drain_seconds="2")

    assert "did not stop within 2s, killing:" in result.stderr, result.stderr[-2000:]
    recorded = lane.pids()
    still = {name: pid for name, pid in recorded.items() if _alive(pid)}
    assert not still, f"a worker outlived the gate: {still}"
    # Escalation is a successful cleanup — loudly, but successfully.
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]


def test_an_interrupted_gate_ends_nonzero_with_nothing_left_running(lane):
    """An interrupted gate is not a passing one, and leaves nothing behind.

    🔴 WHAT THIS DOES NOT PROVE, said plainly. `lane_interrupted` exists
    because `trap lane_stop INT TERM` runs the cleanup and then RESUMES the
    script — it never required the interrupted run to end. On THIS host the
    test cannot tell that repair from the platform: signalling an MSYS bash
    tears down its process tree either way, and the assertions below pass
    against the old trap too (checked). So this is an outcome check — nonzero,
    nothing of ours still running — not evidence about which mechanism
    produced it. On a POSIX host the distinction is real and the trap is what
    provides it.
    """
    env = lane.env_for(scenario_sleep="60")
    for path in lane.pid_files.values():
        path.unlink(missing_ok=True)
    gate_pid_file = lane.tmp_path / "gate.pid"

    started = subprocess.Popen(
        [SH, "-c",
         f'echo $$ > "{gate_pid_file.as_posix()}"\n'
         f'export PATH="{_posix(lane.fake_bin)}:$PATH"\n'
         f'exec sh "$0" "$@"',
         str(ROOT / "scripts" / "gates.sh"), "--quick", "--with-agent-eval"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8",
    )
    try:
        # Interrupt it while the scenario is running, not before the lane
        # has anything to clean up.
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if (gate_pid_file.exists()
                    and len(lane.pids()) == 3
                    and all(_alive(p) for p in lane.pids().values())):
                break
            time.sleep(0.5)
        else:
            raise AssertionError("the lane never started all three processes")
        recorded = lane.pids()
        # 🔴 TERM to the GATE, not INT to its process group. A group signal
        # kills the foreground child too, and `set -e` then ends the script on
        # its own — which is what made the first version of this test pass
        # against the very trap it was meant to catch. Signalled alone, the old
        # `trap lane_stop INT` ran cleanup and let the script CARRY ON to
        # `all gates passed`.
        subprocess.run([SH, "-c", f'kill -TERM {gate_pid_file.read_text().strip()}'],
                       timeout=30)
        out, err = started.communicate(timeout=180)
    finally:
        if started.poll() is None:
            started.kill()

    assert started.returncode != 0, out[-2000:]
    assert "all gates passed" not in out, out[-2000:]
    still = {name: pid for name, pid in recorded.items() if _alive(pid)}
    assert not still, f"the interrupt left these running: {still}"
