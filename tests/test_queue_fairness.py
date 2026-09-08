"""One team's backlog must not become every other team's outage.

🔴 THE DEFECT. The pipeline claim was `order by created_at` across the WHOLE
queue, with no per-team consideration of any kind. A team that connects a busy
repository queues one job per webhook delivery and one per document; every one
of them is older than the next team's first job, so a single team's backlog sat
at the head of the queue and everybody else waited behind it. T12 gave agent
turns a per-team ceiling for exactly this reason and left the pipeline queue —
the one whose depth is driven by an EXTERNAL event rate rather than by members
typing — ordered first-come-first-served.
"""
import psycopg

from pipeline import worker
from shared.config import settings
from shared.db import Role, team_session
from tests._seed import TEAM_A, TEAM_B


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _clear():
    conn = _admin()
    try:
        conn.execute("delete from public.jobs")
    finally:
        conn.close()


def _queue(team_id: str, n: int, prefix: str) -> None:
    with team_session(Role.PIPELINE, team_id) as conn:
        for i in range(n):
            conn.execute(
                "insert into public.jobs (team_id, job_type, payload, dedupe_key)"
                " values (%s,'ingest_github','{}',%s)",
                (team_id, f"{prefix}-{i}"),
            )


def _serve(times: int) -> list[str]:
    """Run the worker `times` times, returning the team of each claimed job."""
    served: list[str] = []
    handlers = {
        "ingest_github": lambda team_id, payload: served.append(str(team_id)),
    }
    for _ in range(times):
        if not worker.run_once(handlers=handlers, worker_id="fairness-test"):
            break
    return served


def test_a_second_team_is_not_stuck_behind_the_first_teams_backlog(seeded):
    """🔴 It was. Every one of team A's five jobs is older than team B's
    only job, and the claim ordered on age alone."""
    _clear()
    _queue(TEAM_A, 5, "a")
    _queue(TEAM_B, 1, "b")

    served = _serve(2)

    assert TEAM_B in served, (
        "team B waited behind the whole of team A's backlog: " + str(served)
    )


def test_the_queue_alternates_rather_than_draining_one_team(seeded):
    _clear()
    _queue(TEAM_A, 3, "a")
    _queue(TEAM_B, 3, "b")

    served = _serve(4)

    assert served.count(TEAM_A) == 2 and served.count(TEAM_B) == 2, served


def test_one_team_alone_is_still_served_oldest_first(seeded):
    """Fairness between teams must not disturb order within one."""
    _clear()
    _queue(TEAM_A, 3, "a")

    keys: list[str] = []
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        expected = [
            r[0] for r in conn.execute(
                "select dedupe_key from public.jobs where team_id=%s"
                " order by created_at", (TEAM_A,),
            ).fetchall()
        ]

    def _record(team_id, payload):
        with team_session(Role.PIPELINE, team_id) as conn:
            keys.append(conn.execute(
                "select dedupe_key from public.jobs where status='processing'"
                " order by picked_at desc limit 1"
            ).fetchone()[0])

    for _ in range(3):
        worker.run_once(handlers={"ingest_github": _record},
                        worker_id="fairness-test")

    assert keys == expected


def test_the_whole_queue_still_drains(seeded):
    """Fair ordering must not leave anything unclaimable."""
    _clear()
    _queue(TEAM_A, 2, "a")
    _queue(TEAM_B, 2, "b")

    assert len(_serve(10)) == 4
