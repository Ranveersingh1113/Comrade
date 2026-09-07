"""Database-backed ordering, leasing and recovery for agent turns."""
from dataclasses import asdict, dataclass
from typing import Any

from shared.db import Role, team_session, user_session

MAX_ATTEMPTS = 3
LEASE_INTERVAL = "5 minutes"
ACTIVE_STATUSES = ("running", "waiting_for_permission", "waiting_for_user")


@dataclass(frozen=True)
class Run:
    id: str
    team_id: str
    requester_id: str
    thread_id: str
    input_message_id: str
    trigger_type: str
    attempts: int
    worker_id: str | None
    input_text: str | None = None


class EnqueuedTurn(str):
    """A run id that preserves the enqueue disposition for the HTTP response."""

    status: str

    def __new__(cls, run_id: str, status: str):
        value = str.__new__(cls, run_id)
        value.status = status
        return value


def enqueue_turn(
    team_id: str, requester_id: str, thread_id: str, text: str, *,
    trigger_type: str = "user", client_request_id: str | None = None,
) -> EnqueuedTurn:
    """Durably append the member message and queued turn in one transaction.

    `client_request_id` identifies the ATTEMPT, not the text. It is what lets a
    retry after an uncertain POST resolve to the turn already accepted instead
    of posting the same question a second time; the disposition comes back as
    "duplicate" so the caller can follow that run rather than start another.
    """
    with user_session(requester_id) as conn:
        row = conn.execute(
            "select run_id, disposition from public.enqueue_agent_turn(%s, %s, %s, %s, %s)",
            (team_id, thread_id, text, trigger_type, client_request_id),
        ).fetchone()
    if row is None:
        raise RuntimeError("enqueue_agent_turn returned no run")
    return EnqueuedTurn(str(row[0]), row[1])


def _recover(conn) -> int:
    return conn.execute("select public.recover_expired_agent_runs()").fetchone()[0]


def recover_expired_runs() -> int:
    """Requeue expired work, or terminally fail it after its final lease."""
    # The definer function is deliberately the only cross-team queue scan.
    # Agent/tool work stays in an RLS-scoped team_session.
    with team_session(Role.AGENT, "00000000-0000-0000-0000-000000000000") as conn:
        return _recover(conn)


def claim_next_run(worker_id: str) -> Run | None:
    """Atomically claim the oldest runnable turn without blocking another worker."""
    with team_session(Role.AGENT, "00000000-0000-0000-0000-000000000000") as conn:
        row = conn.execute(
            "select * from public.claim_next_agent_run(%s)", (worker_id,)
        ).fetchone()
    if row is None:
        return None
    return Run(*(str(value) if index < 6 and value is not None else value for index, value in enumerate(row)))


def renew_lease(run_id: str, worker_id: str) -> bool:
    with team_session(Role.AGENT, "00000000-0000-0000-0000-000000000000") as conn:
        return bool(conn.execute(
            "select public.renew_agent_run_lease(%s, %s)", (run_id, worker_id)
        ).fetchone()[0])


#: Why a stopped turn stopped. The member reads the RUN ROW, not the
#: generator, so a cancelled run with no reason reaches them as a blank halt.
CANCEL_REASON = "stopped by the member who asked for it"


def cancel_run(team_id: str, run_id: str, *, requester_id: str | None) -> bool:
    """Cancellation is terminal, so a worker cannot later claim this run.

    `requester_id` is not optional by accident: ownership is enforced in this
    one statement rather than by reading the run and then writing it, because
    a check in a different transaction from the write is a check that can be
    raced. Pass None only for cancellation with no requester behind it —
    maintenance, not a person.
    """
    owned = "" if requester_id is None else " and requester_id=%s"
    params: tuple = (run_id,) if requester_id is None else (run_id, requester_id)
    with team_session(Role.AGENT, team_id) as conn:
        cur = conn.execute(
                "update public.agent_runs set status='cancelled', finished_at=now(),"
                " lease_expires_at=null, worker_id=null,"
                " last_error=coalesce(last_error, %s)"
                " where id=%s" + owned + " and status in ('queued','running',"
                " 'waiting_for_permission','waiting_for_user')",
                (CANCEL_REASON,) + params,
            )
    return cur.rowcount == 1


def get_run(team_id: str, run_id: str) -> dict[str, Any] | None:
    """Read queue metadata and durable events; worker input is kept separate."""
    with team_session(Role.AGENT, team_id) as conn:
        row = conn.execute(
            "select id, thread_id, requester_id, status, attempts, worker_id, lease_expires_at, finished_at,"
            " last_error from public.agent_runs where id=%s",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        steps = conn.execute(
            "select seq, type, tool, args, response, text from public.agent_steps"
            " where run_id=%s order by seq",
            (run_id,),
        ).fetchall()
    return {
        "id": str(row[0]), "thread_id": str(row[1]),
        "requester_id": str(row[2]) if row[2] else None,
        "status": row[3], "attempts": row[4], "worker_id": row[5],
        "lease_expires_at": row[6], "finished_at": row[7],
        "last_error": row[8],
        "steps": [
            {"seq": s[0], "type": s[1], **({"tool": s[2]} if s[2] else {}),
             **({"args": s[3]} if s[3] is not None else {}),
             **({"response": s[4]} if s[4] is not None else {}),
             **({"text": s[5]} if s[5] is not None else {})}
            for s in steps
        ],
    }


def owns_run(run: Run) -> bool:
    """False means cancellation/recovery took this lease from this worker."""
    with team_session(Role.AGENT, run.team_id) as conn:
        row = conn.execute(
            "select 1 from public.agent_runs where id=%s and worker_id=%s"
            " and status='running' and lease_expires_at >= now()",
            (run.id, run.worker_id),
        ).fetchone()
    return row is not None


def finished_by_worker(run: Run) -> bool:
    """The worker that closed a successful run may persist its reply."""
    with team_session(Role.AGENT, run.team_id) as conn:
        row = conn.execute(
            "select 1 from public.agent_runs where id=%s and worker_id=%s"
            " and status='done'",
            (run.id, run.worker_id),
        ).fetchone()
    return row is not None


def finish_claimed_run(run: Run, status: str, error: str | None = None) -> bool:
    """Only the worker currently holding the lease may close its run."""
    with team_session(Role.AGENT, run.team_id) as conn:
        cur = conn.execute(
            "update public.agent_runs set status=%s, finished_at=now(),"
            " lease_expires_at=null, last_error=coalesce(%s, last_error)"
            " where id=%s and worker_id=%s and status='running'",
            (status, error, run.id, run.worker_id),
        )
    return cur.rowcount == 1


def input_for(run: Run) -> Run:
    """Read the input as its requester, preserving thread RLS for the worker."""
    with user_session(run.requester_id) as conn:
        row = conn.execute("select body from public.messages where id=%s", (run.input_message_id,)).fetchone()
    if row is None:
        raise LookupError(f"input message {run.input_message_id} is unavailable")
    return Run(**(asdict(run) | {"input_text": row[0]}))
