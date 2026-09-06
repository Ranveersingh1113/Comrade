"""A development server outlives the turn that started it, so something has to
own it.

`repo_run` is finite — start, wait, return — and everything about its
containment is enforced by the fact that it ends. A long-running process breaks
that: the turn finishes, the worker may restart, and the only thing that knows
a container exists is a row in Postgres. Written BEFORE the container starts,
because a row we failed to write is a container nobody can name.
"""
import psycopg
import pytest

from agent import processes
from shared.config import settings
from tests._seed import A1, A2, B1, TEAM_A, TEAM_B, as_user


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
def no_docker(monkeypatch):
    """Record what would have been run, without a daemon.

    The containment guarantees are flags in an argv list, so asserting on the
    argv is stronger than booting a container — a test that needs Docker is a
    test that gets skipped on the machine where it matters.
    """
    calls: list[list[str]] = []

    def _fake(argv):
        calls.append(argv)
        # A host that does not have the preview network yet, which is the
        # state every host is in the first time. `inspect` failing is how
        # _ensure_network decides to create it.
        if argv[1:3] == ["network", "inspect"]:
            raise processes.ProcessError("No such network")
        return "c" * 64          # a container id, as `docker run -d` prints

    monkeypatch.setattr(processes, "_docker", _fake)
    return calls


# ---------------------------------------------------------------------------
# The row is the owner
# ---------------------------------------------------------------------------

def test_the_row_exists_before_the_container_does(seeded, admin, monkeypatch, tmp_path):
    """🔴 The ordering that makes cleanup possible at all. If the container is
    started first and the process dies before the insert, we have a running
    container with no id anywhere — unfindable, and holding a port until
    someone notices the host is slow."""
    thread_id = _thread(admin)
    seen: list[str | None] = []

    def _fake(argv):
        if argv[1] == "network":
            return ""            # setting the network up is not the run
        seen.append(admin.execute(
            "select state from public.sandbox_processes where thread_id=%s",
            (thread_id,),
        ).fetchone()[0])
        return "c" * 64

    monkeypatch.setattr(processes, "_docker", _fake)
    processes.start(TEAM_A, thread_id, "npm run dev", root=tmp_path, port=3000)

    assert seen == ["starting"], "the container was started before it was recorded"


def test_a_started_process_is_running_and_carries_its_container(
    seeded, admin, no_docker, tmp_path
):
    thread_id = _thread(admin)
    proc = processes.start(TEAM_A, thread_id, "npm run dev", root=tmp_path, port=3000)

    row = admin.execute(
        "select state, container_id, port, command from public.sandbox_processes"
        " where id=%s", (proc["id"],),
    ).fetchone()
    assert row[0] == "running"
    assert row[1] == "c" * 64
    assert (row[2], row[3]) == (3000, "npm run dev")


def test_a_box_config_never_falls_back_to_the_host_docker_daemon(
    seeded, admin, no_docker, monkeypatch, tmp_path
):
    """Cloud configuration must not quietly run customer code on our host."""
    monkeypatch.setattr(settings, "comrade_sandbox_backend", "box")
    thread_id = _thread(admin)

    with pytest.raises(processes.ProcessError, match="Box previews are not enabled"):
        processes.start(TEAM_A, thread_id, "npm run dev", root=tmp_path, port=3000)

    assert no_docker == []
    assert admin.execute(
        "select count(*) from public.sandbox_processes where thread_id=%s", (thread_id,)
    ).fetchone()[0] == 0


def test_stopping_records_the_stop_rather_than_deleting_the_row(
    seeded, admin, no_docker, tmp_path
):
    """A deleted row is a process that never existed. The audit question after
    an incident is 'what was running', and DELETE cannot answer it."""
    thread_id = _thread(admin)
    proc = processes.start(TEAM_A, thread_id, "npm run dev", root=tmp_path, port=3000)
    processes.stop(TEAM_A, proc["id"])

    row = admin.execute(
        "select state, stopped_at is not null from public.sandbox_processes"
        " where id=%s", (proc["id"],),
    ).fetchone()
    assert row == ("stopped", True)


def test_stopping_a_process_twice_is_not_an_error(seeded, admin, no_docker, tmp_path):
    """The reconciler and a member's request can both arrive."""
    thread_id = _thread(admin)
    proc = processes.start(TEAM_A, thread_id, "npm run dev", root=tmp_path, port=3000)
    processes.stop(TEAM_A, proc["id"])
    processes.stop(TEAM_A, proc["id"])
    assert admin.execute(
        "select count(*) from public.sandbox_processes where state='stopped'"
    ).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Containment
# ---------------------------------------------------------------------------

def test_a_server_runs_detached_but_keeps_every_other_guarantee(
    seeded, admin, no_docker, tmp_path
):
    """The ONLY thing a background process gives up is waiting for it. It is
    still non-root, still capability-free, still resource-capped, and still on
    no network the host can reach."""
    thread_id = _thread(admin)
    processes.start(TEAM_A, thread_id, "npm run dev", root=tmp_path, port=3000)
    argv = no_docker[-1]

    assert "-d" in argv, "a server that blocks the worker is not a server"
    assert "--cap-drop" in argv and "ALL" in argv
    assert "--user" in argv
    assert "--memory" in argv and "--pids-limit" in argv
    assert "--rm" not in argv, (
        "--rm destroys the container's logs on exit, and the logs are the only"
        " evidence of why a server died"
    )


def test_nothing_is_ever_published_to_the_host(seeded, admin, no_docker, tmp_path):
    """🔴 The correction. This first published `-p 127.0.0.1::<port>`, which
    the plan forbids in two separate tasks — and which does not even work in
    the deployed topology, because the API runs in its own container so the
    host's loopback is not the API's. The proxy could not have reached what it
    was meant to proxy.

    The container is reachable only from inside the preview network."""
    thread_id = _thread(admin)
    processes.start(TEAM_A, thread_id, "npm run dev", root=tmp_path, port=3000)
    argv = no_docker[-1]

    assert "-p" not in argv, argv
    assert not [a for a in argv if a.startswith("127.0.0.1:")]
    assert argv[argv.index("--network") + 1] == processes.PREVIEW_NETWORK


def test_the_preview_network_has_no_route_out(seeded, admin, no_docker, tmp_path):
    """`--internal` is what keeps "no network" true for a container that is,
    technically, on a bridge. Dependencies were installed in a phase that had
    the network; running needs none."""
    thread_id = _thread(admin)
    processes.start(TEAM_A, thread_id, "npm run dev", root=tmp_path, port=3000)
    created = [a for a in no_docker if "network" in a and "create" in a]

    assert created, no_docker
    assert "--internal" in created[0]


def test_the_container_name_is_stored_because_it_is_the_address(
    seeded, admin, no_docker, tmp_path
):
    """The proxy dials the container by name through Docker's DNS. Relying on
    the short id resolving instead would work today and break on an upgrade
    with no error message."""
    thread_id = _thread(admin)
    proc = processes.start(TEAM_A, thread_id, "npm run dev", root=tmp_path, port=3000)
    name = admin.execute(
        "select container_name from public.sandbox_processes where id=%s",
        (proc["id"],),
    ).fetchone()[0]
    assert name and name.startswith("comrade-proc-")


@pytest.mark.parametrize("bad", [0, -1, 70000, 22])
def test_a_port_that_cannot_be_previewed_is_refused(seeded, admin, no_docker, bad, tmp_path):
    """22 is in the list deliberately: a process that could publish a
    privileged port is a process that can put something in front of one."""
    thread_id = _thread(admin)
    with pytest.raises(processes.ProcessError):
        processes.start(TEAM_A, thread_id, "npm run dev", root=tmp_path, port=bad)


# ---------------------------------------------------------------------------
# Who can see it
# ---------------------------------------------------------------------------

def test_a_teammate_sees_the_process_and_an_outsider_does_not(
    seeded, admin, no_docker, tmp_path
):
    thread_id = _thread(admin)
    proc = processes.start(TEAM_A, thread_id, "npm run dev", root=tmp_path, port=3000)

    with as_user(A2) as conn:
        assert conn.execute(
            "select id from public.sandbox_processes where id=%s", (proc["id"],)
        ).fetchone() is not None
    with as_user(B1) as conn:
        assert conn.execute(
            "select id from public.sandbox_processes where id=%s", (proc["id"],)
        ).fetchone() is None


def test_a_member_cannot_start_or_stop_one_from_the_browser(
    seeded, admin, no_docker, tmp_path
):
    """A process belongs to the turn that created it. A stop button racing a
    starting container leaves exactly the orphan this table exists to prevent,
    so `authenticated` is granted SELECT and nothing else."""
    thread_id = _thread(admin)
    proc = processes.start(TEAM_A, thread_id, "npm run dev", root=tmp_path, port=3000)
    with as_user(A1) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(
            "update public.sandbox_processes set state='stopped' where id=%s",
            (proc["id"],),
        )


def test_a_process_cannot_be_started_on_another_teams_thread(
    seeded, admin, no_docker, tmp_path
):
    other = _thread(admin, TEAM_B)
    with pytest.raises(psycopg.Error):
        processes.start(TEAM_A, other, "npm run dev", root=tmp_path, port=3000)


# ---------------------------------------------------------------------------
# Reaping
# ---------------------------------------------------------------------------

def test_an_idle_process_is_expired_and_killed(seeded, admin, no_docker, monkeypatch, tmp_path):
    """Nothing reclaims these on its own. A dev server left from a turn three
    hours ago holds a port and a CPU share for as long as the host lives."""
    killed: list[str] = []
    monkeypatch.setattr(processes, "_kill", lambda cid: killed.append(cid))

    thread_id = _thread(admin)
    proc = processes.start(TEAM_A, thread_id, "npm run dev", root=tmp_path, port=3000)
    admin.execute(
        "update public.sandbox_processes set last_seen_at = now() - interval '9 hours'"
        " where id=%s", (proc["id"],),
    )

    assert processes.reap() == 1
    assert killed == ["c" * 64]
    assert admin.execute(
        "select state from public.sandbox_processes where id=%s", (proc["id"],)
    ).fetchone()[0] == "expired"


def test_a_live_process_is_left_alone(seeded, admin, no_docker, monkeypatch, tmp_path):
    """A reaper that kills working processes is worse than no reaper."""
    monkeypatch.setattr(processes, "_kill", lambda cid: None)
    thread_id = _thread(admin)
    processes.start(TEAM_A, thread_id, "npm run dev", root=tmp_path, port=3000)
    assert processes.reap() == 0
