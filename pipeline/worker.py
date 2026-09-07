"""Polling job worker for the document pipeline.

The queue itself is control-plane: claiming the next pending job scans across
all teams, so it runs on a privileged connection (admin here; production would
use a dedicated least-privilege queue role). The actual work runs inside each
handler, team-scoped under team_session(PIPELINE, team_id) — that is where RLS
enforces the data boundary.

Handlers are registered by job_type and receive (team_id, payload).

Run it: `uv run python -m pipeline.worker` — drains the queue, then sweeps
chat->memory (ambient capture), then sleeps and repeats.
"""
import logging
import signal
import threading
import time
from typing import Callable

from shared.db import Role, connect

logger = logging.getLogger(__name__)

Handler = Callable[[str, dict], None]

_HANDLERS: dict[str, Handler] = {}
MAX_ATTEMPTS = 3
LEASE_INTERVAL = "30 minutes"


class PermanentJobError(ValueError):
    """A job failure that cannot succeed by retrying the same payload."""

# Atomic claim: pick the oldest pending job, skipping rows another worker holds.
_CLAIM_SQL = (
    "update public.jobs set status='processing', attempts=attempts+1,"
    f" picked_at=now(), lease_expires_at=now() + interval '{LEASE_INTERVAL}',"
    " finished_at=null where id = ("
    "  select id from public.jobs"
    "  where (status='pending' or (status='processing' and lease_expires_at < now()))"
    f"    and attempts < {MAX_ATTEMPTS}"
    "  order by created_at for update skip locked limit 1"
    ") returning id, team_id, job_type, payload, attempts"
)


def register(job_type: str, handler: Handler) -> None:
    _HANDLERS[job_type] = handler


def _finish(job_id, status: str, error: str | None = None) -> None:
    terminal = status in ("done", "failed")
    with connect(Role.CONTROL) as conn:
        conn.autocommit = True
        conn.execute(
            "update public.jobs set status=%s, last_error=%s,"
            " lease_expires_at=null,"
            " finished_at = case when %s then now() else null end where id=%s",
            (status, error, terminal, job_id),
        )


def _fail_expired_leases() -> None:
    """Terminally fail work abandoned after its final lease expires."""
    with connect(Role.CONTROL) as conn:
        conn.autocommit = True
        conn.execute(
            "update public.jobs set status='failed', finished_at=now(),"
            " lease_expires_at=null,"
            " last_error=coalesce(last_error, 'worker lease expired')"
            " where status='processing' and lease_expires_at < now()"
            " and attempts >= %s",
            (MAX_ATTEMPTS,),
        )


def run_once(handlers: dict[str, Handler] | None = None) -> bool:
    """Claim and process one pending job. Returns False if the queue was empty."""
    handlers = _HANDLERS if handlers is None else handlers
    _fail_expired_leases()
    with connect(Role.CONTROL) as conn:
        conn.autocommit = True
        job = conn.execute(_CLAIM_SQL).fetchone()
    if job is None:
        return False

    job_id, team_id, job_type, payload, attempts = job
    try:
        handler = handlers.get(job_type)
        if handler is None:
            raise ValueError(f"no handler registered for job_type: {job_type}")
        handler(str(team_id), payload or {})
        _finish(job_id, "done")
    except PermanentJobError as exc:
        _finish(job_id, "failed", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - record any transient failure on the job
        retry = attempts < MAX_ATTEMPTS
        _finish(job_id, "pending" if retry else "failed", error=str(exc))
    return True


POLL_SECONDS = 5.0


def tick() -> int:
    """One worker iteration: drain the queue, then sweep chat->memory.

    The sweep runs after the drain so a batch enqueued this tick is picked up
    on the next — keeping each tick short and each job claim fair across
    workers. Returns how many jobs were processed. Sweep failures are logged,
    not fatal: a broken sweep must not stop document jobs from draining.
    """
    # Imported here: chat.py registers its handler via this module, so a
    # module-level import would be circular.
    from pipeline.chat import sweep_chat_compiles

    processed = 0
    while run_once():
        processed += 1
    try:
        swept = sweep_chat_compiles()
        if swept:
            logger.info("chat sweep enqueued %d compile job(s)", len(swept))
    except Exception:  # noqa: BLE001 - sweep is best-effort by design
        logger.exception("chat sweep failed; queue drain unaffected")

    # Same contract: best-effort, never fatal. A reconciler that can stop the
    # queue draining is a reconciler that turns a disk problem into an outage.
    from pipeline.repo_sync import (
        enforce_disk_cap, sweep_orphan_workspaces, sweep_stale_checkouts,
    )

    # Environments a team ASKED for and does not have. Fires only where a
    # leader set env_enabled — the entire difference between this and the
    # version that installed a manifest the moment a repo was connected.
    from pipeline.repo_env import enforce_env_disk_cap, sweep_environments

    # Long-running sandbox processes. NOTHING else reclaims one: a development
    # server started three hours ago holds a port and a CPU share for as long
    # as the host lives, and the turn that started it is long gone.
    from agent.processes import (
        drain_cleanup, reap as reap_processes,
        reconcile as reconcile_processes,
    )

    try:
        sweep_stale_checkouts()
        sweep_environments()
        enforce_env_disk_cap()
        sweep_orphan_workspaces()
        enforce_disk_cap()
    except Exception:  # noqa: BLE001
        logger.exception("workspace sweep failed; queue drain unaffected")

    try:
        # Reconcile FIRST. A server that crashed on its own leaves the row
        # saying `running` forever, and the thread keeps offering a preview
        # link to nothing. Reaping before reconciling would expire rows that
        # had already exited and report work that was never done.
        changed = reconcile_processes()
        if changed:
            logger.info("reconciled %d sandbox process(es) with the daemon", changed)
        reaped = reap_processes()
        if reaped:
            logger.info("reaped %d sandbox process(es) past a lifetime", reaped)
        # Containers whose owning thread was deleted. The evidence outlives the
        # row on purpose (20260907100000); this is what acts on it.
        reclaimed = drain_cleanup()
        if reclaimed:
            logger.info("reclaimed %d orphaned container(s)", reclaimed)
    except Exception:  # noqa: BLE001
        logger.exception("process reconciliation failed; queue drain unaffected")
    return processed


#: Set by SIGTERM. A container stop is not a crash, and the difference is worth
#: keeping: an abandoned run recovers only when its lease expires, which is
#: minutes of a member watching nothing happen. Draining costs one more item's
#: worth of shutdown and skips that entirely.
_stopping = threading.Event()


def _drain_on_signal() -> None:
    """Finish the item in hand, then stop. Second signal is not caught, so an
    operator who means it can still kill the process outright."""
    def _handle(signum, _frame):
        logger.info("signal %s received: draining, will stop after this item", signum)
        _stopping.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):
            # Not the main thread, or a platform without it. A worker that
            # cannot install the handler still works; it just stops abruptly.
            logger.warning("could not install a %s handler", sig)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # Handlers register at import time, so this list IS the wiring — a module
    # missing here is a job type that fails three times and gives up, with
    # "no handler registered" as the only trace.
    #
    # 🔴 repo_sync was missing, and every test passed: pytest imports it, so
    # the handler was registered in the test process and nowhere else. A team
    # connected a repository, four sync jobs failed in under a second, and the
    # setup screen sat on CLONING… for good. tests/test_worker_handlers.py
    # now checks this list against the job types the database permits, in a
    # subprocess, because only a fresh interpreter can tell the difference.
    import pipeline.chat  # noqa: F401
    import pipeline.compiler  # noqa: F401
    import pipeline.github  # noqa: F401
    import pipeline.repo_env  # noqa: F401
    import pipeline.repo_sync  # noqa: F401

    _drain_on_signal()
    logger.info("worker up: polling every %.0fs", POLL_SECONDS)
    while not _stopping.is_set():
        if tick() == 0:
            _stopping.wait(POLL_SECONDS)
    logger.info("worker drained")


if __name__ == "__main__":
    # 🔴 NOT `main()`. THE HANDLER TABLE SPLITS IN TWO IF YOU CALL IT DIRECTLY.
    #
    # `python -m pipeline.worker` — the command in the README — executes this
    # file as the module `__main__`. When a handler module then does
    # `from pipeline.worker import register`, Python does not find that name
    # already imported, so it LOADS THIS FILE A SECOND TIME as
    # `pipeline.worker`. Two module objects, each with its own `_HANDLERS`
    # dict: register() writes to one and the loop reads the other.
    #
    # Every job type failed with "no handler registered" — parse_document,
    # compile_memory, ingest_github, compile_github, sync_repo,
    # build_environment. The whole queue, silently, in the documented way of
    # running it.
    #
    # Nothing caught it because every test calls tick() or run_once() in a
    # process where `pipeline.worker` was imported normally and there is only
    # one copy. The bug exists only under `-m`, which is exactly and only how
    # production starts.
    #
    # Importing main from the canonical module means the loop runs in the same
    # module object register() writes to.
    from pipeline.worker import main as _main

    _main()
