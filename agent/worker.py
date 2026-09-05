"""Durable worker for queued agent turns.

Run with ``uv run python -m agent.worker``. The HTTP process only enqueues;
this process owns the lease while it calls the model.
"""
import logging
import os
import socket
import threading
import time

from agent.run_queue import (
    claim_next_run, finish_claimed_run, finished_by_worker, input_for, owns_run,
    renew_lease,
)
from agent.runtime import run_turn_sync
from shared.db import Role, team_session

logger = logging.getLogger(__name__)
POLL_SECONDS = 1.0
RENEW_SECONDS = 60.0


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _persist_ai_reply(team_id: str, thread_id: str, text: str) -> None:
    if not text:
        return
    with team_session(Role.AGENT, team_id) as conn:
        conn.execute(
            "insert into public.messages (team_id, thread_id, sender_kind, body)"
            " values (%s,%s,'ai',%s)",
            (team_id, thread_id, text),
        )


def _renew_until_stopped(run_id: str, worker_id: str, stopped: threading.Event) -> None:
    while not stopped.wait(RENEW_SECONDS):
        if not renew_lease(run_id, worker_id):
            return


def run_once(worker_id: str | None = None) -> bool:
    """Claim and execute one queued turn. False means the queue was empty."""
    worker_id = worker_id or _worker_id()
    run = claim_next_run(worker_id)
    if run is None:
        return False
    if not owns_run(run):
        return True
    stopped = threading.Event()
    renewer = threading.Thread(
        target=_renew_until_stopped, args=(run.id, worker_id, stopped), daemon=True
    )
    renewer.start()
    try:
        run = input_for(run)
        result = run_turn_sync(
            run.team_id, run.requester_id, run.input_text or "",
            thread_id=run.thread_id, exclude_message_id=run.input_message_id,
            run_id=run.id, worker_id=worker_id, lock_held=True,
        )
        if finished_by_worker(run):
            _persist_ai_reply(run.team_id, run.thread_id, result.get("reply", ""))
    except Exception as exc:  # noqa: BLE001 - failures are durable queue state
        logger.exception("agent run failed: %s", run.id)
        finish_claimed_run(run, "failed", str(exc))
    finally:
        stopped.set()
        renewer.join(timeout=1)
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    worker_id = _worker_id()
    logger.info("agent worker up: %s", worker_id)
    while True:
        if not run_once(worker_id):
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    # Import the canonical module so functions and module globals are shared.
    from agent.worker import main as _main

    _main()
