"""The agent worker executes only claimed, durable turns."""
import subprocess
import sys
from pathlib import Path

import psycopg

from agent.run_queue import Run, claim_next_run, enqueue_turn
from shared.agent_runs import finish_run
from shared.config import settings
from tests._seed import A1, TEAM_A, general_thread

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
    monkeypatch.setattr(worker, "finished_by_worker", lambda _run: True, raising=False)
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


def test_worker_persists_reply_after_runtime_finishes_claim(seeded, monkeypatch):
    """A terminal run still belongs to its worker for its one reply write."""
    from agent import worker

    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        thread_id = general_thread(conn, TEAM_A)
    run_id = enqueue_turn(TEAM_A, A1, thread_id, "what is open?")
    run = claim_next_run("worker-one")
    assert run is not None and run.id == run_id

    monkeypatch.setattr(worker, "claim_next_run", lambda _worker: run)
    monkeypatch.setattr(
        worker, "run_turn_sync",
        lambda *_args, **_kwargs: (
            finish_run(TEAM_A, run.id, "done", worker_id=run.worker_id)
            or {"reply": "Two tasks are open."}
        ),
    )

    assert worker.run_once("worker-one")
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        body = conn.execute(
            "select body from public.messages where thread_id=%s"
            " and sender_kind='ai' order by created_at desc limit 1",
            (thread_id,),
        ).fetchone()
    assert body == ("Two tasks are open.",)


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
