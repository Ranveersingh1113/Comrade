"""The agent worker executes only claimed, durable turns."""
import subprocess
import sys
from pathlib import Path

from agent.run_queue import Run

ROOT = Path(__file__).resolve().parent.parent


def _run() -> Run:
    return Run(
        id="run-1", team_id="team-1", requester_id="user-1", thread_id="thread-1",
        input_message_id="message-1", trigger_type="user", attempts=1,
        worker_id="worker-1", input_text="hello",
    )


def test_worker_executes_claimed_run_and_persists_its_reply(monkeypatch):
    from agent import worker

    run = _run()
    seen = {}
    monkeypatch.setattr(worker, "claim_next_run", lambda _worker: run)
    monkeypatch.setattr(worker, "owns_run", lambda _run: True)
    monkeypatch.setattr(worker, "input_for", lambda _run: run)
    monkeypatch.setattr(worker, "_persist_ai_reply", lambda *args: seen.update(reply=args))
    monkeypatch.setattr(worker, "renew_lease", lambda *_: True)
    monkeypatch.setattr(
        worker, "run_turn_sync",
        lambda *args, **kwargs: seen.update(call=(args, kwargs)) or {"reply": "done"},
    )

    assert worker.run_once("worker-1")
    assert seen["call"] == (
        ("team-1", "user-1", "hello"),
        {"thread_id": "thread-1", "exclude_message_id": "message-1",
         "run_id": "run-1", "worker_id": "worker-1", "lock_held": True},
    )
    assert seen["reply"] == ("team-1", "thread-1", "done")


def test_module_entry_point_uses_the_canonical_worker_module():
    """A fresh ``-m`` interpreter must run the same module mocks import."""
    src = (
        "import runpy, time\n"
        "import agent.worker as canonical\n"
        "fired=[]\n"
        "canonical.run_once=lambda *_: (fired.append(True), False)[1]\n"
        "time.sleep=lambda *_: (_ for _ in ()).throw(SystemExit(0))\n"
        "try: runpy.run_module('agent.worker', run_name='__main__')\n"
        "except SystemExit: pass\n"
        "print('FIRED' if fired else 'SPLIT')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", src], cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "FIRED"
