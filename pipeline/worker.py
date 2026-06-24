"""Polling job worker for the document pipeline.

The queue itself is control-plane: claiming the next pending job scans across
all teams, so it runs on a privileged connection (admin here; production would
use a dedicated least-privilege queue role). The actual work runs inside each
handler, team-scoped under team_session(PIPELINE, team_id) — that is where RLS
enforces the data boundary.

Handlers are registered by job_type and receive (team_id, payload).
"""
from typing import Callable

from shared.db import Role, connect

Handler = Callable[[str, dict], None]

_HANDLERS: dict[str, Handler] = {}
MAX_ATTEMPTS = 3

# Atomic claim: pick the oldest pending job, skipping rows another worker holds.
_CLAIM_SQL = (
    "update public.jobs set status='processing', attempts=attempts+1,"
    " picked_at=now() where id = ("
    "  select id from public.jobs where status='pending'"
    "  order by created_at for update skip locked limit 1"
    ") returning id, team_id, job_type, payload, attempts"
)


def register(job_type: str, handler: Handler) -> None:
    _HANDLERS[job_type] = handler


def _finish(job_id, status: str, error: str | None = None) -> None:
    terminal = status in ("done", "failed")
    with connect(Role.ADMIN) as conn:
        conn.autocommit = True
        conn.execute(
            "update public.jobs set status=%s, last_error=%s,"
            " finished_at = case when %s then now() else null end where id=%s",
            (status, error, terminal, job_id),
        )


def run_once(handlers: dict[str, Handler] | None = None) -> bool:
    """Claim and process one pending job. Returns False if the queue was empty."""
    handlers = _HANDLERS if handlers is None else handlers
    with connect(Role.ADMIN) as conn:
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
    except Exception as exc:  # noqa: BLE001 - record any failure on the job
        retry = attempts < MAX_ATTEMPTS
        _finish(job_id, "pending" if retry else "failed", error=str(exc))
    return True
