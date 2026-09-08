"""Reclaiming what a deleted thread left running.

🔴 THE DEFECT (fix.md F08). The queue that could not empty.

`sandbox_cleanup` exists because deleting a thread cascades the process row
away and takes the container's identity with it. The trigger copies what is
needed to reclaim it; `drain_cleanup` does the reclaiming. Three things stopped
it working, and they compound:

  * It removed the network with a bare `docker network rm`. The preview proxy's
    endpoint is still attached to that network, and Docker refuses to remove a
    network with an endpoint on it — every time, for as long as the proxy
    exists. The normal path (`_remove_network`) detaches the proxy first; the
    drain did not use it.
  * A retry after a partial success failed on the container it had already
    removed, because "no such container" came back as an error.
  * The batch is `order by requested_at limit 50`. A row that fails keeps its
    place at the front of that order forever, so fifty stuck rows consumed
    every pass and nothing behind them was ever reached.

The third is what turns the first two from "some rows retry" into "the queue
never drains again".
"""
import psycopg
import pytest

from agent import processes
from agent.processes import ProcessError
from shared.config import settings
from tests._seed import TEAM_A


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def queue(seeded, admin, monkeypatch):
    """An empty cleanup queue and a configured proxy."""
    admin.execute("delete from public.sandbox_cleanup")
    monkeypatch.setattr(settings, "comrade_preview_proxy_container", "comrade-api")

    def add(*, container="c" * 64, name="comrade-proc-x", network="comrade-prev-x",
            reason="test", attempts=0):
        return str(admin.execute(
            "insert into public.sandbox_cleanup (container_id, container_name,"
            " network, reason, attempts) values (%s,%s,%s,%s,%s) returning id",
            (container, name, network, reason, attempts),
        ).fetchone()[0])

    return add


def _pending(admin) -> int:
    return admin.execute(
        "select count(*) from public.sandbox_cleanup where done_at is null",
    ).fetchone()[0]


# ---------------------------------------------------------------------------

def test_the_network_is_detached_from_the_proxy_before_it_is_removed(
    queue, admin, monkeypatch,
):
    """🔴 The finding's own case. `docker network rm` on a network the proxy
    is still attached to is refused, so this row failed on every pass for as
    long as the deployment had a proxy — which is always."""
    seen: list[list[str]] = []

    def _fake(argv):
        seen.append(argv)
        if argv[1:3] == ["network", "rm"]:
            # Exactly what Docker says while an endpoint is attached.
            if not any(a[1:3] == ["network", "disconnect"] for a in seen):
                raise ProcessError(
                    "error while removing network: network comrade-prev-x id"
                    " has active endpoints"
                )
        return ""

    monkeypatch.setattr(processes, "_docker", _fake)
    queue()

    reclaimed = processes.drain_cleanup()

    assert reclaimed == 1, "the network could not be removed, so nothing was"
    assert _pending(admin) == 0


def test_a_container_already_gone_counts_as_reclaimed(queue, admin, monkeypatch):
    """A retry after a partial success must not fail on the half that worked.
    Otherwise the first interruption makes the row permanently stuck."""
    def _fake(argv):
        if argv[1] == "rm" and argv[2] == "-f":
            raise ProcessError("Error response from daemon: No such container: c")
        return ""

    monkeypatch.setattr(processes, "_docker", _fake)
    queue()

    assert processes.drain_cleanup() == 1
    assert _pending(admin) == 0


def test_a_network_already_gone_counts_as_reclaimed(queue, admin, monkeypatch):
    def _fake(argv):
        if argv[1:3] == ["network", "rm"]:
            # The daemon's own wording, checked against Docker 28. It is
            # NOT "no such network", which is what an analogy with the
            # container message would have produced.
            raise ProcessError(
                "Error response from daemon: network comrade-prev-x not found")
        return ""

    monkeypatch.setattr(processes, "_docker", _fake)
    queue()

    assert processes.drain_cleanup() == 1
    assert _pending(admin) == 0


def test_a_real_daemon_failure_stays_pending_and_is_recorded(
    queue, admin, monkeypatch,
):
    """"Do not suppress daemon failures." A row we could not act on must stay
    in the queue, and say why."""
    def _fake(argv):
        raise ProcessError("Cannot connect to the Docker daemon")

    monkeypatch.setattr(processes, "_docker", _fake)
    cleanup_id = queue()

    assert processes.drain_cleanup() == 0

    attempts, error, done = admin.execute(
        "select attempts, last_error, done_at from public.sandbox_cleanup"
        " where id=%s", (cleanup_id,),
    ).fetchone()
    assert done is None
    assert attempts == 1
    assert error


def test_a_healthy_row_is_not_starved_by_fifty_stuck_ones(
    queue, admin, monkeypatch,
):
    """🔴 The batch is fifty rows ordered by age. Fifty rows that cannot
    succeed keep their place at the front of that order forever, so a row
    queued afterwards is never reached — and after the network defect above,
    fifty stuck rows is the normal state of a busy deployment."""
    for i in range(50):
        queue(container=f"stuck{i}", name=f"comrade-proc-stuck{i}",
              network=f"comrade-prev-stuck{i}", attempts=7)
    healthy = queue(container="healthy", name="comrade-proc-healthy",
                    network="comrade-prev-healthy")

    def _fake(argv):
        if any("stuck" in a for a in argv):
            raise ProcessError("Cannot connect to the Docker daemon")
        return ""

    monkeypatch.setattr(processes, "_docker", _fake)

    processes.drain_cleanup()

    done = admin.execute(
        "select done_at from public.sandbox_cleanup where id=%s", (healthy,),
    ).fetchone()[0]
    assert done is not None, "the healthy row never came up for its turn"


def test_a_row_that_keeps_failing_is_not_retried_on_every_pass(
    queue, admin, monkeypatch,
):
    """Putting fresh rows first must not turn a starved queue into a hot loop:
    a row the daemon has already refused waits before it asks again."""
    def _fake(argv):
        raise ProcessError("Cannot connect to the Docker daemon")

    monkeypatch.setattr(processes, "_docker", _fake)
    cleanup_id = queue()

    processes.drain_cleanup()
    first = admin.execute(
        "select attempts from public.sandbox_cleanup where id=%s", (cleanup_id,),
    ).fetchone()[0]
    processes.drain_cleanup()
    second = admin.execute(
        "select attempts from public.sandbox_cleanup where id=%s", (cleanup_id,),
    ).fetchone()[0]

    assert first == 1
    assert second == 1, "the same failing row was hammered twice in a row"


def test_the_wait_expires_so_a_recoverable_failure_is_retried(
    queue, admin, monkeypatch,
):
    """The backoff is a delay, not a graveyard. A daemon that comes back must
    get the work finished."""
    calls = {"n": 0}

    def _fake(argv):
        calls["n"] += 1
        raise ProcessError("Cannot connect to the Docker daemon")

    monkeypatch.setattr(processes, "_docker", _fake)
    cleanup_id = queue()
    processes.drain_cleanup()

    admin.execute(
        "update public.sandbox_cleanup set next_attempt_at = now() -"
        " interval '1 hour' where id=%s", (cleanup_id,),
    )
    monkeypatch.setattr(processes, "_docker", lambda argv: "")

    assert processes.drain_cleanup() == 1
    assert _pending(admin) == 0
