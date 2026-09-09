"""Workers saying they are alive, on their own clock.

🔴 THE DEFECT (fix.md F33). `/ready` decided a deployment could serve a turn
from queue AGE — runs queued over ten minutes, jobs pending over ten minutes.
On a quiet deployment both queues are empty, so a stack with BOTH WORKERS
STOPPED reported itself ready, indistinguishable from a healthy idle one. The
first member to send a message finds out, which is precisely what readiness
exists to prevent.

"Is work stuck" and "is anybody here" are different questions. This answers the
second, and it answers it whether or not there is anything to do.

It also carries what the worker can DO. The API deliberately holds no Docker
socket — granting one to the internet-facing process would turn a
request-handling bug into a host compromise — so it cannot discover whether
the sandbox provider is answering. The process that holds the socket reports
that, and readiness reads the report.
"""
import logging
import os
import socket
import threading
import time
from typing import Any

from shared.db import Role, connect

logger = logging.getLogger(__name__)

#: How often a worker writes. Comfortably shorter than STALE_SECONDS so a
#: single slow loop does not read as a dead process.
BEAT_SECONDS = 30.0

#: How old a heartbeat may be before the worker is presumed gone. Three missed
#: beats: one is a slow tick, three is a pattern.
STALE_SECONDS = 120


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def beat(kind: str, *, capabilities: dict[str, Any] | None = None,
         role: Role = Role.CONTROL) -> None:
    """Record that this worker is alive and what it can do.

    Never raises. A worker that cannot write its heartbeat is a worker with a
    database problem, and it has better ways to report that than by dying in
    its own bookkeeping — readiness will notice the silence, which is the
    point.
    """
    from psycopg.types.json import Json

    try:
        with connect(role) as conn:
            conn.autocommit = True
            conn.execute(
                "insert into public.worker_heartbeats"
                " (worker_kind, worker_id, capabilities) values (%s,%s,%s)"
                " on conflict (worker_kind, worker_id) do update"
                "   set last_seen_at = now(),"
                "       capabilities = excluded.capabilities",
                (kind, worker_id(), Json(capabilities or {})),
            )
    except Exception as exc:  # noqa: BLE001 - never fatal
        from shared.errors import safe_error

        logger.warning("could not record heartbeat: %s", safe_error(exc))


class Beater:
    """Beat at most every `BEAT_SECONDS`, called from a loop that runs often."""

    def __init__(self, kind: str, *, role: Role = Role.CONTROL) -> None:
        self.kind = kind
        self.role = role
        self._last = 0.0

    def maybe(self, capabilities: Any = None) -> bool:
        """Beat if it is time. `capabilities` may be a CALLABLE, evaluated only
        when a beat actually happens — probing the sandbox means a subprocess,
        and running one on every pass of a tight loop to throw the answer away
        is the kind of cost that gets a heartbeat removed."""
        now = time.monotonic()
        if now - self._last < BEAT_SECONDS:
            return False
        self._last = now
        beat(self.kind,
             capabilities=capabilities() if callable(capabilities) else capabilities,
             role=self.role)
        return True


class Pulse:
    """Liveness on a thread of its own.

    🔴 THE DEFECT (fix.md F39). `Beater` is called FROM the work loop, and both
    workers beat and then block — `run_once` is a whole agent turn,  `tick` a
    whole pipeline batch. STALE_SECONDS is 120, so a turn that holds its slots
    for longer than that stops the beats and readiness declares live workers
    missing. The check added so a quiet deployment could not lie about being
    ready acquired the opposite failure: a BUSY deployment lying about being
    dead.

    Liveness has to come from something that is not the thing doing the work.

    WHAT A BEAT DOES NOT MEAN. That this process's current job is progressing.
    A wedged job is diagnosed through its LEASE expiring, which `/ready`
    reports separately, and conflating the two would hide exactly the case
    leases exist to catch. This says the process is up. That is all it says.
    """

    def __init__(self, kind: str, *, capabilities: Any = None,
                 role: Role = Role.CONTROL) -> None:
        self.kind = kind
        self._capabilities = capabilities
        self._role = role
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "Pulse":
        if self._thread is not None:
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"heartbeat-{self.kind}", daemon=True,
        )
        self._thread.start()
        return self

    def _run(self) -> None:
        while True:
            try:
                beat(
                    self.kind,
                    capabilities=(self._capabilities()
                                  if callable(self._capabilities)
                                  else self._capabilities),
                    role=self._role,
                )
            except Exception as exc:  # noqa: BLE001 - a pulse outlives surprises
                # `beat` already refuses to raise; this is for a failing
                # CAPABILITY probe, which shells out. A worker that went
                # permanently silent because one probe threw would be reported
                # dead while it is working — the defect this class exists for,
                # arriving through the new door.
                from shared.errors import safe_error

                logger.warning("heartbeat pulse failed: %s", safe_error(exc))
            # Read at wait time, so the interval is the module's and not a copy
            # taken at construction.
            if self._stop.wait(BEAT_SECONDS):
                return

    def stop(self) -> None:
        """Stop beating, promptly. A drain that waited out BEAT_SECONDS would
        make every deployment slower for a heartbeat nobody reads by then."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "Pulse":
        return self.start()

    def __exit__(self, *exc: object) -> bool:
        self.stop()
        return False


def live_workers(within: int = STALE_SECONDS) -> dict[str, list[dict]]:
    """Which workers have reported recently, by kind."""
    with connect(Role.CONTROL) as conn:
        rows = conn.execute(
            "select worker_kind, worker_id, capabilities,"
            "       extract(epoch from now() - last_seen_at)::int"
            "  from public.worker_heartbeats"
            " where last_seen_at > now() - make_interval(secs => %s)"
            " order by worker_kind, last_seen_at desc",
            (within,),
        ).fetchall()
    out: dict[str, list[dict]] = {}
    for kind, wid, capabilities, age in rows:
        out.setdefault(kind, []).append(
            {"worker_id": wid, "capabilities": capabilities, "age_seconds": age}
        )
    return out
