"""A container that started, on a row that never learned its id.

🔴 THE DEFECT (fix.md F07). `start` writes the row and the INTENDED CONTAINER
NAME first, launches, and then writes the id back. T06 built it that way
precisely so a crash between the launch and the write-back leaves evidence:
the row names exactly what to look for.

Nothing looked. Every reclaimer filtered on the id:

  * `reconcile()` selects `where ... and container_id is not null`
  * `stop()` does `if container_id: _remove_container(...)`
  * the expiry sweep does `if container_id: _kill(...)`
  * the delete trigger queues cleanup only `if old.container_id is not null`

So the one state the design was built to survive was the one state nothing
could act on. The container runs, holding a port and a CPU, on a network
nobody removes, and the row says `starting` forever.

The second half is a distinction the reconciler did not make. `_inspect_state`
returned None for a container Docker has never heard of AND for a daemon that
would not answer, and the caller writes "the container is gone" for None. A
daemon restart therefore rewrote every live preview in the deployment as
failed — losing the ids, which is the one thing that made them findable.
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


def _thread(admin, team_id=TEAM_A):
    return str(admin.execute(
        "select id from public.threads where team_id=%s and title='General'",
        (team_id,),
    ).fetchone()[0])


@pytest.fixture
def crashed(seeded, admin, monkeypatch, tmp_path):
    """A process that launched and then lost its worker before the write-back.

    Produced by making the write-back itself fail, rather than by inserting a
    handmade row: the point is the state the real code path leaves behind.
    """
    thread_id = _thread(admin)
    monkeypatch.setattr(settings, "comrade_preview_proxy_container", "comrade-api")
    launched: list[list[str]] = []

    def _fake(argv):
        launched.append(argv)
        if argv[1:3] == ["network", "inspect"]:
            return "true"
        return "c" * 64

    monkeypatch.setattr(processes, "_docker", _fake)

    real_session = processes.team_session
    state = {"crash": False}

    class _Boom(Exception):
        pass

    def _session(role, team_id=None):
        if state["crash"]:
            raise _Boom("the worker died between the launch and the write-back")
        return real_session(role, team_id)

    # Everything up to and including `docker run` happens for real; the
    # session that would record the id does not.
    def _crash_after_launch(argv):
        launched.append(argv)
        if argv[1:3] == ["network", "inspect"]:
            return "true"
        if argv[1] == "run":
            state["crash"] = True
        return "c" * 64

    monkeypatch.setattr(processes, "_docker", _crash_after_launch)
    monkeypatch.setattr(processes, "team_session", _session)

    with pytest.raises(_Boom):
        processes.start(TEAM_A, thread_id, "npm run dev", root=tmp_path, port=3000)

    state["crash"] = False
    monkeypatch.setattr(processes, "team_session", real_session)
    monkeypatch.setattr(processes, "_docker", _fake)

    row = admin.execute(
        "select id, container_id, container_name, state"
        " from public.sandbox_processes where team_id=%s"
        " order by started_at desc limit 1", (TEAM_A,),
    ).fetchone()
    assert row is not None, "the row was never written"
    assert row[1] is None, "this fixture is meant to leave the id unwritten"
    assert row[2], "the name is the only handle left; without it there is nothing"
    return {"id": str(row[0]), "name": row[2], "thread": thread_id,
            "calls": launched}


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def test_reconcile_finds_the_container_by_the_name_the_row_reserved(
    crashed, admin, monkeypatch,
):
    """🔴 The finding's own case. A live container, a row that cannot see it."""
    def _inspect(ref):
        assert ref == crashed["name"], f"looked up {ref!r}, not the reserved name"
        return {"running": False, "exit_code": 137}

    monkeypatch.setattr(processes, "_inspect_state", _inspect)

    changed = processes.reconcile()

    state, code = admin.execute(
        "select state, exit_code from public.sandbox_processes where id=%s",
        (crashed["id"],),
    ).fetchone()
    assert changed >= 1
    assert state == "exited"
    assert code == 137


def test_a_recovered_row_learns_the_id_so_it_stops_being_a_special_case(
    crashed, admin, monkeypatch,
):
    """Finding it by name once is a rescue; recording the id is the repair.
    Otherwise every later pass re-does the same lookup for the same row."""
    monkeypatch.setattr(processes, "_inspect_state",
                        lambda ref: {"running": True, "exit_code": None,
                                     "container_id": "d" * 64})

    processes.reconcile()

    container_id = admin.execute(
        "select container_id from public.sandbox_processes where id=%s",
        (crashed["id"],),
    ).fetchone()[0]
    assert container_id == "d" * 64


def test_a_container_confirmed_absent_is_recorded_as_lost(
    crashed, admin, monkeypatch,
):
    """`docker run` may never have got as far as a container. That is a lost
    process, not a finished one, and it must not stay `starting` forever."""
    monkeypatch.setattr(processes, "_inspect_state", lambda ref: None)

    processes.reconcile()

    state = admin.execute(
        "select state from public.sandbox_processes where id=%s",
        (crashed["id"],),
    ).fetchone()[0]
    assert state == "failed"


def test_a_daemon_that_will_not_answer_does_not_rewrite_every_row(
    seeded, admin, monkeypatch, tmp_path,
):
    """🔴 The second half. `_inspect_state` returned None both for "no such
    container" and for "the daemon refused", and the caller writes "the
    container is gone from the daemon" for None. A daemon restart therefore
    marked every live preview failed and dropped the ids — losing the only
    handle on containers that were still running."""
    thread_id = _thread(admin)
    monkeypatch.setattr(settings, "comrade_preview_proxy_container", "comrade-api")
    monkeypatch.setattr(
        processes, "_docker",
        lambda argv: "true" if argv[1:3] == ["network", "inspect"] else "c" * 64,
    )
    started = processes.start(TEAM_A, thread_id, "npm run dev",
                              root=tmp_path, port=3000)

    def _daemon_down(argv):
        raise ProcessError("Cannot connect to the Docker daemon at unix:///var/run/docker.sock")

    monkeypatch.setattr(processes, "_docker", _daemon_down)

    processes.reconcile()

    state, container_id = admin.execute(
        "select state, container_id from public.sandbox_processes where id=%s",
        (started["id"],),
    ).fetchone()
    assert state == "running", "a daemon outage was recorded as a dead container"
    assert container_id, "the id was thrown away, and with it the only handle"


# ---------------------------------------------------------------------------
# Stopping, expiring, deleting
# ---------------------------------------------------------------------------

def test_stopping_a_recovered_process_reclaims_it_by_name(
    crashed, admin, monkeypatch,
):
    removed: list[str] = []
    monkeypatch.setattr(processes, "_remove_container",
                        lambda ref: removed.append(ref))

    processes.stop(TEAM_A, crashed["id"])

    assert removed == [crashed["name"]]
    state = admin.execute(
        "select state from public.sandbox_processes where id=%s",
        (crashed["id"],),
    ).fetchone()[0]
    assert state == "stopped"


def test_expiry_kills_a_recovered_process_rather_than_only_its_network(
    crashed, admin, monkeypatch,
):
    """The sweep skipped the kill when the id was absent and removed the
    network anyway — which detaches the container from anything that could
    reach it while leaving it running."""
    admin.execute(
        "update public.sandbox_processes"
        "   set last_seen_at = now() - interval '48 hours',"
        "       started_at   = now() - interval '48 hours' where id=%s",
        (crashed["id"],),
    )
    killed: list[str] = []
    monkeypatch.setattr(processes, "_kill", lambda ref: killed.append(ref))

    processes.reap()

    assert killed == [crashed["name"]]


def test_deleting_the_thread_still_queues_the_container_for_cleanup(
    crashed, admin,
):
    """🔴 The trigger's own filter. `if old.container_id is not null` meant a
    deleted thread dropped the row AND the name — a permanent leak, which is
    the exact failure sandbox_cleanup was created to prevent."""
    admin.execute("delete from public.messages where thread_id=%s",
                  (crashed["thread"],))
    admin.execute("delete from public.threads where id=%s", (crashed["thread"],))

    row = admin.execute(
        "select container_id, container_name, network from public.sandbox_cleanup"
        " where done_at is null order by requested_at desc limit 1",
    ).fetchone()
    assert row is not None, "the thread went and took the container's name with it"
    assert row[1] == crashed["name"]


def test_cleanup_reclaims_by_name_when_there_is_no_id(seeded, admin, monkeypatch):
    # Other tests leave pending rows, and the drain takes fifty at a time.
    admin.execute("delete from public.sandbox_cleanup")
    admin.execute(
        "insert into public.sandbox_cleanup (container_id, container_name,"
        " network, reason) values (null, 'comrade-proc-orphan',"
        " 'comrade-prev-orphan', 'test')",
    )
    removed: list[str] = []
    monkeypatch.setattr(processes, "_remove_container",
                        lambda ref: removed.append(ref))
    monkeypatch.setattr(processes, "_docker", lambda argv: "")

    processes.drain_cleanup()

    assert "comrade-proc-orphan" in removed
    done = admin.execute(
        "select done_at from public.sandbox_cleanup"
        " where container_name='comrade-proc-orphan'",
    ).fetchone()[0]
    assert done is not None


def test_a_name_that_matches_nothing_removes_nothing_else(crashed, monkeypatch):
    """"Never remove an unrelated container on a name mismatch." A lookup that
    finds nothing must reclaim nothing — not fall back to a prefix, a
    substring, or the newest container."""
    asked: list[list[str]] = []

    def _record(argv):
        asked.append(argv)
        raise ProcessError("Error: No such object: comrade-proc-whatever")

    monkeypatch.setattr(processes, "_docker", _record)

    processes.stop(TEAM_A, crashed["id"])

    for argv in asked:
        assert crashed["name"] in argv or argv[1] == "network", (
            f"reclaimed something that was not the reserved name: {argv}"
        )
