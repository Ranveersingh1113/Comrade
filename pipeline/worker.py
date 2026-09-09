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
import os
import signal
import socket
import threading
import time
from typing import Callable

from shared.db import Role, connect, team_session
from shared.errors import redact, safe_error
from shared.heartbeat import Pulse

from shared import observability

logger = logging.getLogger(__name__)

Handler = Callable[[str, dict], None]

_HANDLERS: dict[str, Handler] = {}
MAX_ATTEMPTS = 3
LEASE_INTERVAL = "30 minutes"


class PermanentJobError(ValueError):
    """A job failure that cannot succeed by retrying the same payload."""

#: How long a retry waits before it is claimable again, and the ceiling on
#: that wait. A retry that is instantly claimable is not a retry: it burns the
#: attempt against a thing that has had no time to change.
RETRY_BACKOFF_SECONDS = 30
MAX_BACKOFF_SECONDS = 900
#: How often a claim is renewed while its handler runs.
RENEW_SECONDS = 60.0
#: How many jobs one tick takes before it stops to do maintenance. Unbounded
#: draining meant a team ingesting steadily starved every sweep forever.
MAX_DRAIN_BATCH = 20

# Atomic claim: pick the oldest available job, skipping rows another worker holds.
_CLAIM_SQL = (
    "update public.jobs set status='processing', attempts=attempts+1,"
    f" picked_at=now(), lease_expires_at=now() + interval '{LEASE_INTERVAL}',"
    " finished_at=null, worker_id=%s where id = ("
    "  select id from public.jobs"
    "  where ((status='pending' and available_at <= now())"
    "         or (status='processing' and lease_expires_at < now()))"
    f"    and attempts < {MAX_ATTEMPTS}"
    # 🔴 This was `order by created_at` across the WHOLE queue, with no
    # per-team consideration at all. A team that connects a busy repository
    # queues one job per webhook delivery and one per document, and every one
    # of them is older than the next team's first job — so one team's backlog
    # sat at the head of the queue and everybody else waited behind it. T12
    # gave agent turns a per-team ceiling for exactly this reason and left
    # this queue, whose depth is driven by an EXTERNAL event rate rather than
    # by members typing, first-come-first-served.
    #
    # Round-robin by the team served longest ago; a team never served yet
    # sorts first. Order WITHIN a team is still oldest-first, which is the
    # ordering that carries meaning — deliveries about the same repository
    # have to be ingested in the order they happened.
    "  order by (select max(served.picked_at) from public.jobs served"
    "            where served.team_id = jobs.team_id"
    "              and served.picked_at is not null) asc nulls first,"
    "           created_at"
    "  for update skip locked limit 1"
    ") returning id, team_id, job_type, attempts"
)


def register(job_type: str, handler: Handler) -> None:
    _HANDLERS[job_type] = handler


def _backoff_seconds(attempts: int) -> int:
    """Wait longer each time, up to a ceiling.

    A fixed delay hammers a broken dependency at a steady rate; no delay at all
    spends every attempt before anything could have recovered.
    """
    return min(RETRY_BACKOFF_SECONDS * (2 ** max(attempts - 1, 0)),
               MAX_BACKOFF_SECONDS)


def _finish(
    job_id, status: str, error: str | None = None, *,
    worker_id: str | None = None, retry_in: int = 0,
) -> None:
    """Close a job — but only if this worker still holds its claim.

    🔴 This used to be `where id = %s`. A worker whose lease had expired, and
    whose job another worker had already reclaimed, still stamped its result
    over the new claim: two workers, one job, and the second one's work thrown
    away by the first one's late answer.
    """
    # Redacted HERE rather than at each caller: this is the boundary where an
    # error becomes durable, and a guard at the boundary covers the callers
    # nobody has written yet. Postgres attaches `DETAIL: Failing row contains
    # (...)` — the whole row — to a constraint violation, and this column is
    # readable across every team by `comrade_control`. See shared/errors.py.
    error = redact(error) if error else error
    terminal = status in ("done", "failed")
    with connect(Role.CONTROL) as conn:
        conn.autocommit = True
        conn.execute(
            "update public.jobs set status=%s, last_error=%s,"
            " lease_expires_at=null,"
            # Kept on a terminal row as provenance — which worker did this —
            # and cleared on a retry, because a job going back on the queue
            # belongs to whoever claims it next.
            " worker_id = case when %s then worker_id else null end,"
            " available_at = now() + make_interval(secs => %s),"
            " finished_at = case when %s then now() else null end"
            " where id=%s and worker_id is not distinct from %s",
            (status, error, terminal, retry_in, terminal, job_id, worker_id),
        )


def renew_job_lease(job_id, worker_id: str | None) -> bool:
    """Push the lease out while the handler is still working.

    A flat lease with no renewal declares any honestly-long job abandoned
    while it is still running, and a second worker then does it again.
    """
    with connect(Role.CONTROL) as conn:
        conn.autocommit = True
        row = conn.execute(
            "update public.jobs set lease_expires_at = now() + interval"
            f" '{LEASE_INTERVAL}' where id=%s and status='processing'"
            " and worker_id is not distinct from %s returning 1",
            (job_id, worker_id),
        ).fetchone()
    return row is not None


def _renew_until_done(job_id, worker_id: str | None, done: threading.Event) -> None:
    while not done.wait(RENEW_SECONDS):
        if not renew_job_lease(job_id, worker_id):
            return


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


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def run_once(
    handlers: dict[str, Handler] | None = None, worker_id: str | None = None,
) -> bool:
    """Claim and process one available job. False means nothing was claimable."""
    handlers = _HANDLERS if handlers is None else handlers
    worker_id = worker_id or _worker_id()
    _fail_expired_leases()
    with connect(Role.CONTROL) as conn:
        conn.autocommit = True
        job = conn.execute(_CLAIM_SQL, (worker_id,)).fetchone()
    if job is None:
        return False

    job_id, team_id, job_type, attempts = job
    # 🔴 The claim used to return the payload, and the claim runs as the CONTROL
    # role — the one role deliberately not scoped to a team, because a single
    # sweeper serves every team. `enqueue_github_event` queues the whole parsed
    # webhook body, so a private repository's pull request descriptions and
    # review comments were readable under one cross-team credential. Read it
    # here instead, as the team's own pipeline role, where `pl_jobs` confines
    # the row to `current_team()`.
    with team_session(Role.PIPELINE, team_id) as conn:
        row = conn.execute(
            "select payload from public.jobs where id=%s", (job_id,)
        ).fetchone()
    if row is None:
        # The row was claimed a moment ago, so the only way it is invisible now
        # is that the scoping is wrong. Running the handler on an empty payload
        # would turn that into a confusing KeyError three attempts later;
        # failing here says what actually happened.
        _finish(job_id, "failed", "job row not visible to the pipeline role",
                worker_id=worker_id)
        return True
    payload = row[0] or {}   # a NULL payload is legitimate for some job types
    # The claim is held for as long as the handler runs, not for a fixed
    # window guessed in advance.
    done = threading.Event()
    renewer = threading.Thread(
        target=_renew_until_done, args=(job_id, worker_id, done), daemon=True
    )
    renewer.start()
    # A worker slot handles one team's job and then another's, so this is
    # scoped to the job rather than set once for the process. Everything the
    # handler logs — and everything the modules it calls log — is attributable
    # without any of them knowing they are inside a job.
    try:
        with observability.log_context(
            team_id=team_id, job_id=job_id, job_type=job_type,
        ):
            handler = handlers.get(job_type)
            if handler is None:
                raise ValueError(f"no handler registered for job_type: {job_type}")
            handler(str(team_id), payload or {})
            _finish(job_id, "done", worker_id=worker_id)
    except PermanentJobError as exc:
        # It will never succeed, so scheduling it again is only noise.
        _finish(job_id, "failed", error=safe_error(exc), worker_id=worker_id)
    except Exception as exc:  # noqa: BLE001 - record any transient failure on the job
        retry = attempts < MAX_ATTEMPTS
        _finish(
            job_id, "pending" if retry else "failed", error=safe_error(exc),
            worker_id=worker_id,
            retry_in=_backoff_seconds(attempts) if retry else 0,
        )
    finally:
        done.set()
        renewer.join(timeout=1)
    return True


POLL_SECONDS = 5.0

#: Sweeps run on their OWN clock, not once per tick.
#:
#: 🔴 They used to run every tick, which was survivable only because a tick
#: drained the whole queue first and therefore happened rarely. Bounding the
#: drain (MAX_DRAIN_BATCH) would otherwise have turned a five-second poll into
#: a five-second full disk scan and Docker reconciliation — fixing starvation
#: by replacing it with a stampede.
CHAT_SWEEP_SECONDS = 30.0
WORKSPACE_SWEEP_SECONDS = 300.0
PROCESS_SWEEP_SECONDS = 30.0

_last_swept: dict[str, float] = {}


def _due(name: str, interval: float) -> bool:
    """True at most once per `interval`, and always on the first call."""
    now = time.monotonic()
    last = _last_swept.get(name)
    if last is not None and now - last < interval:
        return False
    _last_swept[name] = now
    return True


def _reset_sweep_timers() -> None:
    """Make every sweep due again. For tests, and for a fresh process."""
    _last_swept.clear()


def tick() -> int:
    """One worker iteration: take a bounded batch of jobs, then sweep.

    The batch is bounded rather than drained to empty, so maintenance keeps
    its turn no matter how much work is queued. The sweep runs after the batch
    so a batch enqueued this tick is picked up on the next — keeping each tick
    short and each job claim fair across workers. Returns how many jobs were
    processed. Sweep failures are logged, not fatal: a broken sweep must not
    stop document jobs from draining.
    """
    # Imported here: chat.py registers its handler via this module, so a
    # module-level import would be circular.
    from pipeline.chat import sweep_chat_compiles

    processed = 0
    # 🔴 BOUNDED. This was `while run_once()`, which drains the queue to
    # EMPTY before anything below runs — so a team ingesting steadily meant
    # the chat sweep, the disk cap and the sandbox reconciler never ran at
    # all. Maintenance that only happens when the system is idle is
    # maintenance that never happens on a busy system.
    for _ in range(MAX_DRAIN_BATCH):
        # Shutdown means finish what is in hand, not start more.
        #
        # 🔴 This read `if _stopping.is_set() and processed`, and `processed`
        # is 0 at the top of a batch — so a stop that arrived while the worker
        # was idle claimed ONE MORE job before noticing. That job then had the
        # whole shutdown grace period to finish or be killed mid-flight, and a
        # job killed mid-flight waits out its entire lease before anyone redoes
        # it. The comment above said the right thing; the condition did not.
        if _stopping.is_set():
            break
        if not run_once():
            break
        processed += 1
    if _due("chat", CHAT_SWEEP_SECONDS):
        try:
            from pipeline.compaction import sweep_thread_compaction

            # Threads that have outgrown the agent's context window. On the
            # same clock as the chat sweep: both are about a conversation
            # having moved on without memory keeping up.
            compacted = sweep_thread_compaction()
            if compacted:
                logger.info("queued %d thread compaction(s)", len(compacted))
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

    if _due("workspace", WORKSPACE_SWEEP_SECONDS):
        try:
            sweep_stale_checkouts()
            sweep_environments()
            enforce_env_disk_cap()
            sweep_orphan_workspaces()
            enforce_disk_cap()
        except Exception:  # noqa: BLE001
            logger.exception("workspace sweep failed; queue drain unaffected")

    if _due("process", PROCESS_SWEEP_SECONDS):
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
            # Containers whose owning thread was deleted. The evidence outlives
            # the row on purpose (20260907100000); this is what acts on it.
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


def main() -> None:
    observability.setup("pipeline-worker")
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
    import pipeline.compaction  # noqa: F401
    import pipeline.compiler  # noqa: F401
    import pipeline.ci  # noqa: F401
    import pipeline.github  # noqa: F401
    import pipeline.repo_env  # noqa: F401
    import pipeline.repo_sync  # noqa: F401

    _drain_on_signal()
    # 🔴 Readiness used to infer this worker's existence from queue AGE, so on
    # a quiet deployment a stopped worker looked exactly like a healthy idle
    # one. It says so itself now (fix.md F33) — and from its OWN THREAD, because
    # beating from this loop meant a batch longer than STALE_SECONDS silenced it
    # and readiness called a working worker dead (fix.md F39).
    with Pulse("pipeline"):
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
