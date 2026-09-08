"""Two workers claiming for one team at the same instant.

🔴 THE DEFECT (fix.md F11). `claim_next_agent_run` takes a per-THREAD advisory
lock, which stops two workers taking the same thread. It does nothing about two
workers taking DIFFERENT threads of the same team: each transaction counts that
team's running runs from its own snapshot, both see the same number, and both
admit. The per-team ceiling `comrade_agent_max_running_per_team` was therefore
advice under exactly the condition it exists for — concurrent workers.

The existing ceiling test claims SEQUENTIALLY (`first = claim(...)` then
`second = claim(...)`), so the second call always saw the first already
committed. It could not have caught this.

Every test here drives two real connections through a barrier, because the
finding is about database behaviour under contention and cannot be established
by reading the SQL.
"""
import threading

import psycopg
import pytest

from agent.run_queue import enqueue_turn
from shared.config import settings
from tests._seed import A1, A2, B1, TEAM_A, TEAM_B

#: How many times to run each race. A scheduler that happens to serialise two
#: threads once will not do it ten times running.
ATTEMPTS = 10


def _thread(team_id: str, title: str = "General") -> str:
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        row = conn.execute(
            "select id from public.threads where team_id=%s and title=%s",
            (team_id, title),
        ).fetchone()
    assert row is not None, f"no {title!r} thread in {team_id}"
    return str(row[0])


def _new_thread(team_id: str, title: str) -> str:
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        row = conn.execute(
            "insert into public.threads (team_id, title, visibility, kind,"
            " created_by) values (%s,%s,'team','discussion',%s) returning id",
            (team_id, title, A1),
        ).fetchone()
        conn.commit()
    return str(row[0])


def _clear_runs() -> None:
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        # Consent first: deleting a run sets `consent_queue.agent_run_id` to
        # null, and `trg_consent_provenance_guard` refuses that — a record of
        # which run asked for an action is not something a delete may quietly
        # rewrite. The guard is right; the fixture has to clear in order.
        conn.execute("delete from public.consent_queue")
        conn.execute("delete from public.agent_runs")
        conn.commit()


def _claim_together(worker_ids, max_per_team: int):
    """Call the claim function from N connections released at one instant.

    Direct connections rather than the pool: two pooled borrows can be the
    same connection, which would serialise the race being tested and produce a
    confident pass for the wrong reason.
    """
    barrier = threading.Barrier(len(worker_ids))
    results: dict[str, object] = {}
    errors: list[BaseException] = []

    def _one(worker_id: str) -> None:
        try:
            with psycopg.connect(settings.comrade_db_url_admin) as conn:
                conn.autocommit = False
                barrier.wait(timeout=10)
                row = conn.execute(
                    "select id from public.claim_next_agent_run(%s, %s)",
                    (worker_id, max_per_team),
                ).fetchone()
                conn.commit()
                results[worker_id] = row[0] if row else None
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=_one, args=(w,)) for w in worker_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    return results


# ---------------------------------------------------------------------------

def test_two_simultaneous_claims_cannot_both_take_the_last_slot(seeded):
    """🔴 They could. Different threads, one team, one slot left: each
    transaction counted the team's running runs from its own snapshot, saw
    zero, and admitted."""
    busy = _thread(TEAM_A)
    other = _new_thread(TEAM_A, "Also busy")

    for attempt in range(ATTEMPTS):
        _clear_runs()
        enqueue_turn(TEAM_A, A1, busy, "one")
        enqueue_turn(TEAM_A, A2, other, "two")

        claimed = _claim_together(["worker-one", "worker-two"], max_per_team=1)

        admitted = [v for v in claimed.values() if v is not None]
        assert len(admitted) == 1, (
            f"attempt {attempt}: {len(admitted)} runs admitted for a team with"
            " one slot; the per-team ceiling is not enforced under contention"
        )


def test_contention_never_exceeds_a_larger_ceiling(seeded):
    """Three workers, a ceiling of two. The bug is not specific to a ceiling
    of one, and neither is the fix."""
    threads = [_thread(TEAM_A)] + [
        _new_thread(TEAM_A, f"Busy {i}") for i in range(3)
    ]

    for attempt in range(ATTEMPTS):
        _clear_runs()
        for i, thread_id in enumerate(threads):
            enqueue_turn(TEAM_A, A1 if i % 2 else A2, thread_id, f"turn {i}")

        claimed = _claim_together(
            ["w1", "w2", "w3", "w4"], max_per_team=2,
        )

        admitted = [v for v in claimed.values() if v is not None]
        assert len(admitted) <= 2, (
            f"attempt {attempt}: {len(admitted)} admitted against a ceiling of 2"
        )


def test_two_workers_still_cannot_take_the_same_thread(seeded):
    """The property that already held has to keep holding: whatever serialises
    the team must not weaken per-thread exclusivity."""
    busy = _thread(TEAM_A)

    for attempt in range(ATTEMPTS):
        _clear_runs()
        enqueue_turn(TEAM_A, A1, busy, "one")

        claimed = _claim_together(["worker-one", "worker-two"], max_per_team=5)

        admitted = [v for v in claimed.values() if v is not None]
        assert len(admitted) == 1, (
            f"attempt {attempt}: one queued run was claimed {len(admitted)} times"
        )


def test_another_team_still_gets_through_during_contention(seeded):
    """Serialising one team's admission must not serialise the deployment.
    A fix that made every claim take one global lock would pass the tests
    above and destroy throughput."""
    a_busy = _thread(TEAM_A)
    a_other = _new_thread(TEAM_A, "Also busy")
    b_thread = _thread(TEAM_B)

    for attempt in range(ATTEMPTS):
        _clear_runs()
        enqueue_turn(TEAM_A, A1, a_busy, "one")
        enqueue_turn(TEAM_A, A2, a_other, "two")
        enqueue_turn(TEAM_B, B1, b_thread, "theirs")

        claimed = _claim_together(["w1", "w2", "w3"], max_per_team=1)

        admitted = [v for v in claimed.values() if v is not None]
        assert len(admitted) == 2, (
            f"attempt {attempt}: expected one run per team, got {len(admitted)}"
        )
