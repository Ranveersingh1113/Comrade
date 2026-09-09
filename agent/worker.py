"""Durable worker for queued agent turns.

Run with ``uv run python -m agent.worker``. The HTTP process only enqueues;
this process owns the lease while it calls the model.
"""
import logging
import os
import signal
import socket
import threading
import time

from agent.run_queue import (
    claim_next_run, finish_claimed_run, finished_by_worker, input_for, owns_run,
    renew_lease,
)
from agent.runtime import run_turn_sync
from shared.config import settings
from agent.sandbox import probe_capabilities
from shared.errors import safe_error
from shared.heartbeat import Pulse
from shared.db import Role, team_session

from shared import observability

logger = logging.getLogger(__name__)
POLL_SECONDS = 1.0
RENEW_SECONDS = 60.0

#: Set by SIGTERM. A container stop is not a crash, and the difference is worth
#: keeping: an abandoned run recovers only when its lease expires, which is
#: minutes of a member watching nothing happen. Draining costs one more item's
#: worth of shutdown and skips that entirely.
_stopping = threading.Event()


def _drain_on_signal() -> None:
    """Finish the item in hand, then stop.

    The FIRST signal drains. The second is left to the default handler, so an
    operator who means it can still kill the process outright.

    🔴 The line above used to say that and it was not true. `signal.signal`
    installs a PERSISTENT handler — it is not reset after delivery — so every
    subsequent SIGTERM was caught and swallowed exactly like the first, and a
    worker wedged inside a long job could not be stopped with anything short of
    SIGKILL. An escape hatch that is documented and absent is worse than one
    that was never claimed, because it is the thing an operator reaches for
    when the first attempt did not work.
    """
    def _handle(signum, _frame):
        logger.info("signal %s received: draining, will stop after this item", signum)
        # Hand this signal back to the default handler before doing anything
        # else: from here on a second one terminates.
        try:
            signal.signal(signum, signal.SIG_DFL)
        except (ValueError, OSError):
            pass
        _stopping.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):
            # Not the main thread, or a platform without it. A worker that
            # cannot install the handler still works; it just stops abruptly.
            logger.warning("could not install a %s handler", sig)



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
        finish_claimed_run(run, "failed", safe_error(exc))
    finally:
        stopped.set()
        renewer.join(timeout=1)
    return True


#: Shared by the slots: one process, one heartbeat. Each slot has its own
#: claim identity, but they are all the same worker as far as "is anybody
#: here" is concerned.


def _loop(worker_id: str) -> None:
    """One claim slot, for the life of the process.

    Wrapped so a slot that hits something unexpected comes back rather than
    disappearing: a thread that returns here is capacity the deployment
    silently no longer has, and nothing would say so.
    """
    while not _stopping.is_set():
        try:
            # 🔴 (fix.md F39) The heartbeat used to be beaten HERE, and
            # `run_once` below is a whole agent turn. A turn holding every slot
            # for longer than STALE_SECONDS stopped the beats and readiness
            # called the workers missing. Liveness now comes from a Pulse in
            # main(), on its own thread, so being busy cannot look like being
            # dead.
            if not run_once(worker_id):
                _stopping.wait(POLL_SECONDS)
        except Exception:  # noqa: BLE001 - a slot must outlive its surprises
            logger.exception("worker slot %s failed; continuing", worker_id)
            _stopping.wait(POLL_SECONDS)


def main() -> None:
    """Run a bounded number of turns at once.

    🔴 This was a single loop running one turn to completion before claiming
    the next, so a long turn anywhere in the deployment made every other team
    wait behind it. Nothing in the queue required that: the claim already
    guarantees one active run per THREAD, which is the ordering guarantee that
    matters, and a per-team ceiling (claim_next_agent_run) stops one team
    taking every slot now that there is more than one.

    Threads rather than an async rework: each turn is a blocking model call
    with blocking DB work around it, the leasing is already per-run, and every
    slot needs its own identity — the lease fence is by worker id, so two slots
    in one process must not be mistaken for each other.
    """
    observability.setup("agent-worker")
    base = _worker_id()
    _drain_on_signal()
    slots = max(settings.comrade_agent_concurrency, 1)
    logger.info("agent worker up: %s (%d slot(s))", base, slots)
    threads = [
        threading.Thread(target=_loop, args=(f"{base}#{i}",), name=f"turn-{i}")
        for i in range(slots)
    ]
    # ONE pulse for the process, not one per slot: liveness is a fact about
    # this process, and a per-slot beat would fall silent exactly when the
    # slots are busy — which is the defect (fix.md F39).
    #
    # It carries what this worker can EXECUTE with. The API deliberately holds
    # no Docker socket, so this is the only process that can answer that, and
    # the probe is passed UNCALLED because it shells out.
    with Pulse("agent", capabilities=probe_capabilities, role=Role.AGENT):
        for thread in threads:
            thread.start()
        # Joined rather than left daemon: shutdown means "stop claiming, finish
        # what is in hand", and a process that exits while a slot is mid-turn
        # abandons a run that then waits out its whole lease before recovery.
        for thread in threads:
            thread.join()
    logger.info("agent worker drained: %s", base)


if __name__ == "__main__":
    # Import the canonical module so functions and module globals are shared.
    from agent.worker import main as _main

    _main()
