"""Supervising a process that outlives the turn that started it.

`repo_run` is finite and that is what makes it safe to be careless about: it
starts a container, waits, and returns, so nothing can be left behind. A
development server breaks the assumption. The turn ends, the worker may
restart, and the only thing that knows the container exists is a row here.

THE ORDER IS THE DESIGN. The row is written BEFORE `docker run`, and the
container id is written back after. A row we never wrote is a container nobody
can name — holding a port and a CPU share until a human notices the host is
slow — while a row in `starting` with no container id is merely an interrupted
creation, which the reconciler can find precisely because it exists.

Everything `repo_run` guarantees still holds: non-root, no capabilities, a
read-only root filesystem, memory/PID caps, and `.git` masked. The one thing
given up is waiting.
"""
import logging
import subprocess
import uuid
from pathlib import Path

from psycopg import errors as pg_errors

from agent.sandbox import (
    DEPS_MOUNT, MOUNT, SANDBOX_UID, VENV, _SECURITY_FLAGS, _git_mask,
    deps_env,
)
from shared.config import settings
from shared.errors import safe_error
from shared.db import Role, team_session

logger = logging.getLogger(__name__)

#: How long a process may go unattended before the reconciler kills it.
#: Generous — someone may reasonably leave a preview up across a lunch break —
#: but finite, because nothing else ever reclaims one.
IDLE_HOURS = 8

#: Ports a preview may use. Below 1024 is privileged, and a process able to
#: publish one is a process able to put something in front of a real service.
MIN_PORT, MAX_PORT = 1024, 65535

DOCKER_TIMEOUT = 120
LOG_CHARS = 8_000

#: One network PER PROCESS, and the only thing attached to it besides the
#: container is the proxy.
#:
#: 🔴 This replaced two wrong designs in a row. First `--network bridge` plus
#: `-p 127.0.0.1::<port>`, which published an unauthenticated development
#: server on a host port and could not work anyway, since the API is in its own
#: container so the host loopback is not the API's. Then a single shared
#: `comrade-preview` network — which fixed the port but put EVERY team's
#: development server on one segment, able to reach each other and able to
#: reach the API container that was also on it. A preview could call Comrade's
#: own API from inside the sandbox.
#:
#: A per-process `--internal` network has neither problem: no route out, no
#: siblings, and the proxy is attached to each network individually.
_NETWORK_PREFIX = "comrade-prev-"


def network_for(process_id: str) -> str:
    """This process's own network. Derived, so it cannot collide."""
    return f"{_NETWORK_PREFIX}{uuid.UUID(str(process_id)).hex}"


class ProcessError(Exception):
    """The process could not be started, stopped, or read."""


def _docker(argv: list[str]) -> str:
    """Run a docker command and return stdout. A seam, so the containment can
    be asserted without a daemon."""
    proc = subprocess.run(  # noqa: S603 - fixed argv, never a shell string
        argv, capture_output=True, text=True, timeout=DOCKER_TIMEOUT,
        errors="replace",
    )
    if proc.returncode != 0:
        raise ProcessError((proc.stderr or proc.stdout or "").strip()[:800])
    return proc.stdout.strip()


def _kill(container_id: str) -> None:
    """Best effort, and deliberately quiet: the reconciler must finish its pass
    even when one container is already gone."""
    for command in (["docker", "kill", container_id],
                    ["docker", "rm", "-f", container_id]):
        try:
            subprocess.run(  # noqa: S603 - fixed argv
                command, capture_output=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("could not %s %s: %s", command[1], container_id[:12], exc)


def _ensure_network(process_id: str) -> str:
    """Create this process's own network, attach the proxy, and CHECK it.

    `--internal` is the load-bearing flag: no gateway, so the container has no
    route to the internet, to host services, or to a metadata endpoint. The
    inspect afterwards is not ceremony — "a network with this name exists" is
    not "this network has no route out", and only the second one contains
    anything.
    """
    proxy = (settings.comrade_preview_proxy_container or "").strip()
    if not proxy:
        raise ProcessError(
            "previews are not configured: COMRADE_PREVIEW_PROXY_CONTAINER is"
            " unset, so there is no way to give the proxy access to a private"
            " preview network."
        )
    network = network_for(process_id)
    try:
        _docker(["docker", "network", "create", "--internal", network])
    except ProcessError as exc:
        if "already exists" not in str(exc):
            raise

    # Read back what was actually created. Fail closed if it is not internal:
    # a pre-existing network with the right name and the wrong policy would
    # otherwise silently give a preview the internet.
    detail = _docker([
        "docker", "network", "inspect", "-f", "{{.Internal}}", network,
    ])
    if detail.strip().lower() not in ("true", ""):
        raise ProcessError(
            f"{network} exists but is not internal; refusing to start a preview"
            " on a network with a route out."
        )
    _docker(["docker", "network", "connect", network, proxy])
    return network


def _absent(exc: Exception) -> bool:
    """Did Docker say the thing is not there?

    Reclaiming something that is already gone is the goal, reached early — so
    this is a SUCCESS, not an error (fix.md F08). It matters on retries: a
    drain interrupted after removing the container failed forever afterwards
    on the container it had already removed.
    """
    return any(marker in str(exc).lower() for marker in _ABSENT)


def drop_network(network: str) -> None:
    """Detach the proxy, then remove the network. Raises if it is still there.

    🔴 (fix.md F08) The cleanup drain called `docker network rm` directly. The
    preview proxy's endpoint is still attached to that network and Docker
    refuses to remove a network with active endpoints — so every deleted
    thread's network failed on every pass, for as long as the deployment had a
    proxy, which is always. The detach is not a nicety; it is the step that
    makes removal possible.
    """
    proxy = (settings.comrade_preview_proxy_container or "").strip()
    if proxy:
        try:
            _docker(["docker", "network", "disconnect", "-f", network, proxy])
        except ProcessError as exc:
            # Not attached, or already gone. Either way the removal below is
            # the thing that decides whether this worked.
            if not _absent(exc):
                logger.warning("could not detach proxy from %s: %s", network, exc)
    try:
        _docker(["docker", "network", "rm", network])
    except ProcessError as exc:
        if not _absent(exc):
            raise


def _remove_network(process_id: str) -> None:
    """Give the network back. One per process is one resource per process, and
    left behind they accumulate until Docker runs out of address space — which
    surfaces as unrelated containers failing to start.

    Best effort: the callers here are already recording a state change and a
    stranded network must not turn a stop into a failure. The cleanup drain
    wants the opposite and calls `drop_network` directly.
    """
    try:
        drop_network(network_for(process_id))
    except ProcessError as exc:
        logger.warning("could not remove the network for %s: %s", process_id, exc)


def _run_argv(
    name: str, root: Path, command: str, port: int | None, network: str,
    deps: str | None = None,
) -> list[str]:
    """The container line for a detached process.

    `-d` and no `--rm`, and both are deliberate. Detached because a server that
    blocks the worker is not a server. No `--rm` because the container's logs
    are the only evidence of why a server died, and `--rm` destroys them at
    exactly the moment they become interesting.

    NO port is published. The container listens on its declared port inside an
    internal network, and the proxy — on that same network — is the only thing
    that can reach it. Publishing to the host would put an unauthenticated
    development server on a host port, which is the thing the proxy exists to
    prevent.
    """
    return [
        "docker", "run", "-d", "--name", name,
        # The internal preview network, and NO published port. The proxy is on
        # the same network and dials this container by name; nothing else can
        # reach it, and the container has no route out. See PREVIEW_NETWORK.
        "--network", network,
        "--read-only",
        # SIZED. /tmp is a tmpfs, which is memory: an unbounded one lets a
        # development server fill the host's RAM by writing a file, and this
        # one lives for hours rather than for one command.
        "--tmpfs", f"/tmp:size={settings.comrade_sandbox_tmp_mb}m",
        *_git_mask(root),
        *_SECURITY_FLAGS,
        "--user", f"{SANDBOX_UID}:{SANDBOX_UID}",
        "-v", f"{root}:{MOUNT}",
        # 🔴 The dependencies, which a preview did not have. repo_run mounts
        # this and a preview did not, so `npm run dev` — the entire reason
        # previews exist — failed on missing modules for any project with
        # dependencies. READ-ONLY, like the finite-command path: a server that
        # can write to what setup installed changes what the next run imports.
        *deps_env(deps),
        "-w", MOUNT,
        settings.comrade_sandbox_image,
        "sh", "-lc", command,
    ]


def _admit(team_id: str) -> None:
    """Refuse a new process when the team or the host is already full.

    A preview holds a container, a network and a CPU share for hours. Without
    this, one team starting servers in a loop takes the host and nothing
    downstream refuses them — the reaper only runs later, by which time the
    damage is done.

    Counted across every team for the host limit, which is why it reads as
    CONTROL: the agent role is scoped to one team and cannot see the total.
    """
    from shared.db import connect

    with connect(Role.CONTROL) as conn:
        conn.autocommit = True
        mine, total = conn.execute(
            "select count(*) filter (where team_id = %s), count(*)"
            "  from public.sandbox_processes"
            " where state in ('starting','running')",
            (team_id,),
        ).fetchone()
    if mine >= settings.comrade_max_processes_per_team:
        raise ProcessError(
            f"this team already has {mine} processes running, which is the"
            " limit. Stop one before starting another."
        )
    if total >= settings.comrade_max_processes_total:
        raise ProcessError(
            "the host is running as many sandbox processes as it allows."
            " Try again shortly."
        )


def start(
    team_id: str, thread_id: str, command: str, *,
    root: Path, port: int | None = None, deps: str | None = None,
    agent_run_id: str | None = None,
) -> dict:
    """Start a long-running process for this thread and record it.

    `root` is the thread's working tree, resolved by the CALLER — the same way
    repo_run takes it from `_root(tool_context)`. This module supervises
    containers; deciding which checkout a thread owns is the repo layer's job,
    and doing it here would make every supervision test need a git repository.
    """
    if settings.comrade_sandbox_backend != "docker":
        raise ProcessError(
            "Box previews are not enabled yet; Comrade will not fall back to"
            " the host Docker daemon."
        )
    if port is not None and not (MIN_PORT <= port <= MAX_PORT):
        raise ProcessError(
            f"{port} cannot be previewed. Use a port between {MIN_PORT} and"
            f" {MAX_PORT}; below {MIN_PORT} is privileged."
        )
    if not command.strip():
        raise ProcessError("no command given")

    _admit(team_id)

    # Written FIRST, and the INTENDED CONTAINER NAME with it.
    #
    # 🔴 The name used to be generated after the insert and written back after
    # the launch, which left a window: a worker that died between `docker run`
    # and the write-back left a container running under a name no row had ever
    # seen. Unfindable, because reconciliation works from the row. Deciding the
    # name up front closes it — a crash now leaves a row that names exactly
    # what to look for.
    name = f"comrade-proc-{uuid.uuid4().hex}"
    with team_session(Role.AGENT, team_id) as conn:
        row = conn.execute(
            "insert into public.sandbox_processes"
            " (team_id, thread_id, agent_run_id, command, port, container_name)"
            " values (%s,%s,%s,%s,%s,%s) returning id",
            (team_id, thread_id, agent_run_id, command, port, name),
        ).fetchone()
    process_id = str(row[0])

    network = None
    try:
        network = _ensure_network(process_id)
        container_id = _docker(
            _run_argv(name, root, command, port, network, deps)
        )
    except ProcessError as exc:
        # A start that died after the network existed must not leave it: one
        # network per process means one leak per failure.
        if network:
            _remove_network(process_id)
        _finish(team_id, process_id, "failed", detail=safe_error(exc))
        raise

    with team_session(Role.AGENT, team_id) as conn:
        conn.execute(
            "update public.sandbox_processes"
            "   set container_id=%s, container_name=%s, state='running',"
            "       last_seen_at=now()"
            " where id=%s and team_id=%s",
            (container_id, name, process_id, team_id),
        )
    return {"id": process_id, "state": "running", "port": port, "command": command}


def _finish(
    team_id: str, process_id: str, state: str, *,
    detail: str | None = None, exit_code: int | None = None,
) -> None:
    with team_session(Role.AGENT, team_id) as conn:
        conn.execute(
            "update public.sandbox_processes"
            "   set state=%s, detail=%s, exit_code=%s, stopped_at=now()"
            " where id=%s and team_id=%s"
            # Terminal states are terminal. Without this a late reconciler pass
            # would move a process a member stopped into 'expired'.
            "   and state in ('starting','running')",
            (state, detail, exit_code, process_id, team_id),
        )


def stop(team_id: str, process_id: str) -> dict:
    """Stop a process. Safe to call twice — the reconciler and a member's
    request can both arrive, and the second must not be an error."""
    with team_session(Role.AGENT, team_id) as conn:
        row = conn.execute(
            "select container_id, container_name, state"
            " from public.sandbox_processes where id=%s and team_id=%s",
            (process_id, team_id),
        ).fetchone()
    if row is None:
        raise ProcessError("no such process in this thread.")
    container_id, container_name, state = row
    if state not in ("starting", "running"):
        return {"id": process_id, "state": state}
    # Confirm the removal BEFORE recording it. A row that says stopped while
    # the container runs is the false signal this whole table exists to avoid,
    # so a failure here propagates and stays retryable.
    ref = handle(container_id, container_name)
    if ref:
        _remove_container(ref)
    _remove_network(process_id)
    _finish(team_id, process_id, "stopped")
    return {"id": process_id, "state": "stopped"}


def logs(team_id: str, process_id: str) -> dict:
    """The tail of what the process printed, clipped and marked as untrusted.

    Clipped because this is pasted into the next model call and a server's
    output is unbounded — one noisy request loop would spend a team's whole
    token budget on log lines.
    """
    from pipeline.parsers import spotlight

    with team_session(Role.AGENT, team_id) as conn:
        row = conn.execute(
            "select container_id, state from public.sandbox_processes"
            " where id=%s and team_id=%s",
            (process_id, team_id),
        ).fetchone()
    if row is None:
        raise ProcessError("no such process in this thread.")
    container_id, state = row
    if not container_id:
        return {"state": state, "output": "", "detail": "it never started."}
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv
            ["docker", "logs", "--tail", "200", container_id],
            capture_output=True, text=True, timeout=DOCKER_TIMEOUT,
            errors="replace",
        )
        text = (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProcessError(f"could not read the logs: {exc}") from exc
    clipped = text[-LOG_CHARS:]
    return {"state": state, "output": spotlight(clipped),
            "truncated": len(text) > LOG_CHARS}


def reap() -> int:
    """Kill processes nobody has attended to, across every team.

    Runs as CONTROL: this is cross-team maintenance and it needs container ids
    and timestamps, never a command's output. Returns how many were reaped so
    a caller can log a number rather than a shrug.
    """
    from shared.db import connect

    reaped = 0
    with connect(Role.CONTROL) as conn:
        conn.autocommit = True
        rows = conn.execute(
            "select id, container_id, container_name from public.sandbox_processes"
            " where state in ('starting','running')"
            # Two clocks, and both are ceilings. Idle catches what nobody is
            # using; absolute catches what is busy forever, which idle alone
            # never would.
            "   and (last_seen_at < now() - make_interval(hours => %s)"
            "        or started_at < now() - make_interval(hours => %s))",
            (IDLE_HOURS, MAX_LIFETIME_HOURS),
        ).fetchall()
        for process_id, container_id, container_name in rows:
            ref = handle(container_id, container_name)
            if ref:
                _kill(ref)
            # 🔴 The kill was skipped when the id was null and the network was
            # removed anyway — which detaches a running container from
            # everything that could reach it and leaves it running.
            _remove_network(str(process_id))
            try:
                conn.execute(
                    "update public.sandbox_processes"
                    "   set state='expired', stopped_at=now(),"
                    "       detail='idle for longer than the preview lifetime'"
                    " where id=%s and state in ('starting','running')",
                    (process_id,),
                )
            except pg_errors.Error as exc:
                # Reported, never swallowed: a reconciler that cannot record
                # what it did will try to kill the same container every pass.
                logger.error("could not close process %s: %s", process_id, exc)
                continue
            reaped += 1
    return reaped


# ---------------------------------------------------------------------------
# Truthful lifecycle (T06)
# ---------------------------------------------------------------------------

#: The hard ceiling, regardless of activity.
#:
#: 🔴 Idle expiry alone is not a bound. A preview someone keeps refreshing — or
#: a server that polls itself — resets last_seen_at forever and runs until the
#: host does. Being busy is not a reason to run indefinitely.
MAX_LIFETIME_HOURS = 24


#: What Docker says when the thing simply is not there. Everything else it can
#: fail with — a daemon that is down, a socket permission, a timeout — is a
#: failure to ANSWER, and answering "gone" for those is how a daemon restart
#: rewrites every live preview as dead.
#: Taken from the daemon rather than guessed. Checked against Docker 28:
#:   docker rm -f X       -> "No such container: X"
#:   docker inspect X     -> "No such object: X"
#:   docker network rm X  -> "network X not found"        <- NOT "no such"
#: The network wording is the one that would have been got wrong by analogy.
_ABSENT = ("no such object", "no such container", "no such image",
           "not found")


def _inspect_state(ref: str) -> dict | None:
    """What Docker says about a container, or None if it has never heard of it.

    `ref` is an id or a NAME. The name is the handle a crash between
    `docker run` and the write-back leaves behind, and it is the only one
    (fix.md F07).

    None and "exited" are different answers and the caller treats them
    differently: a container Docker cannot find was removed underneath us,
    which is a lost process rather than a finished one.

    🔴 So is "the daemon would not answer", and this returned None for that
    too. The caller writes 'the container is gone from the daemon' for None —
    so a daemon restart marked every running preview failed and dropped their
    ids, losing the only handle on containers that were still running. That
    case now RAISES, and the reconciler leaves the row alone.
    """
    try:
        raw = _docker([
            "docker", "inspect",
            "-f", "{{.State.Running}} {{.State.ExitCode}} {{.Id}}",
            ref,
        ]).strip()
    except ProcessError as exc:
        if any(marker in str(exc).lower() for marker in _ABSENT):
            return None
        raise
    if not raw:
        return None
    running, _, rest = raw.partition(" ")
    code, _, container_id = rest.partition(" ")
    return {"running": running.lower() == "true",
            "exit_code": int(code) if code.strip().lstrip("-").isdigit() else None,
            "container_id": container_id.strip() or None}


def handle(container_id: str | None, container_name: str | None) -> str | None:
    """What to ask Docker about, given what the row knows.

    🔴 (fix.md F07) Every reclaimer used the id and skipped the row when it was
    null — `reconcile`, `stop`, the expiry sweep and the delete trigger alike.
    That is precisely the state a crash between `docker run` and the write-back
    leaves, which is the state the name was reserved up front to survive. The
    one case the design was built for was the one nothing acted on.

    The id wins when both are known: it cannot be recycled, and a name can be
    (a later process could reuse a name only if Docker had let the first one
    go, but preferring the id removes the question).
    """
    return container_id or container_name


def _remove_container(container_id: str) -> None:
    """Stop and remove, and RAISE if it did not go.

    `docker kill` returning non-zero because the container is already dead is
    fine; `docker rm -f` failing is not, because the caller is about to record
    that the process stopped. "I asked Docker to stop it" is not "it stopped",
    and a row that says stopped while the container runs is exactly the false
    signal this table exists to avoid.
    """
    try:
        _docker(["docker", "kill", container_id])
    except ProcessError:
        pass          # already dead is a fine reason for kill to fail
    try:
        _docker(["docker", "rm", "-f", container_id])
    except ProcessError as exc:
        # A container Docker has never heard of is one nobody has to reclaim.
        # This is what makes a retry after a partial success possible at all.
        if not _absent(exc):
            raise


def touch(team_id: str, process_id: str) -> None:
    """Record that this process was just used.

    Called on every accepted preview request. Without it, idle expiry kills a
    preview someone is actively looking at, because nothing recorded the use.
    """
    with team_session(Role.AGENT, team_id) as conn:
        conn.execute(
            "update public.sandbox_processes set last_seen_at = now()"
            " where id=%s and team_id=%s and state in ('starting','running')",
            (process_id, team_id),
        )


def reconcile() -> int:
    """Make the row agree with Docker. Returns how many rows changed.

    A development server that crashed on its own leaves the row saying
    `running` forever, and the thread keeps offering a preview link to nothing.
    Nothing else ever notices: the process that started it is long gone.
    """
    from shared.db import connect

    changed = 0
    with connect(Role.CONTROL) as conn:
        conn.autocommit = True
        rows = conn.execute(
            # 🔴 `and container_id is not null` was here, and it excluded the
            # one state this reconciler exists for: a container that started
            # and whose row never learned its id.
            "select id, container_id, container_name from public.sandbox_processes"
            " where state in ('starting','running')"
            "   and (container_id is not null or container_name is not null)"
        ).fetchall()
        for process_id, container_id, container_name in rows:
            ref = handle(container_id, container_name)
            try:
                state = _inspect_state(ref)
            except ProcessError as exc:
                # The daemon would not answer. That is not evidence about this
                # container, and writing 'gone' for it would throw away the id.
                logger.warning("could not inspect %s: %s", ref, exc)
                continue
            if state is not None and state.get("container_id") and not container_id:
                # Finding it by name is the rescue; recording the id is the
                # repair, so later passes are ordinary.
                conn.execute(
                    "update public.sandbox_processes set container_id=%s"
                    " where id=%s and container_id is null",
                    (state["container_id"], process_id),
                )
                changed += 1
            if state is None:
                # Docker has never heard of it. Removed underneath us, or it
                # never started — either way the process is lost, not finished.
                conn.execute(
                    "update public.sandbox_processes"
                    "   set state='failed', stopped_at=now(),"
                    "       detail='the container is gone from the daemon'"
                    " where id=%s and state in ('starting','running')",
                    (process_id,),
                )
                changed += 1
            elif not state["running"]:
                conn.execute(
                    "update public.sandbox_processes"
                    "   set state='exited', exit_code=%s, stopped_at=now()"
                    " where id=%s and state in ('starting','running')",
                    (state["exit_code"], process_id),
                )
                changed += 1
    return changed


def drain_cleanup() -> int:
    """Reclaim containers whose owning row was deleted.

    The evidence outlives the thread on purpose (see the trigger in
    20260907100000): a cascade that removed the row removed the only record of
    a running container, which kept running where nothing could find it.

    Failures stay pending and are counted, so a daemon that is briefly down
    does not silently drop the work.
    """
    from shared.db import connect

    reclaimed = 0
    with connect(Role.CONTROL) as conn:
        conn.autocommit = True
        rows = conn.execute(
            "select id, container_id, container_name, network"
            " from public.sandbox_cleanup"
            # 🔴 `order by requested_at` alone. A row that fails keeps its
            # place at the FRONT of that order forever, so fifty stuck rows
            # consumed every 50-row batch and nothing queued behind them was
            # ever reached. Attempts first: a row nobody has tried is never
            # behind one that has already refused.
            #
            # And the wait, so putting fresh rows first does not turn a
            # starved queue into a hot loop against a daemon that keeps
            # saying no.
            " where done_at is null and next_attempt_at <= now()"
            " order by attempts, requested_at limit 50"
        ).fetchall()
        for cleanup_id, container_id, container_name, network in rows:
            ref = handle(container_id, container_name)
            try:
                if ref:
                    _remove_container(ref)
                if network:
                    # The normal path, which detaches the proxy first. A bare
                    # `network rm` is refused while an endpoint is attached.
                    drop_network(network)
            except ProcessError as exc:
                conn.execute(
                    "update public.sandbox_cleanup"
                    "   set attempts = attempts + 1, last_error = %s,"
                    # Doubling from half a minute, capped at an hour: long
                    # enough that a daemon outage costs nothing, short enough
                    # that a container is not left running for a day.
                    "       next_attempt_at = now() + least("
                    "         interval '1 hour',"
                    "         make_interval(secs => 30 * power(2,"
                    "           least(attempts, 7))::int))"
                    " where id=%s",
                    (safe_error(exc), cleanup_id),
                )
                continue
            conn.execute(
                "update public.sandbox_cleanup set done_at = now() where id=%s",
                (cleanup_id,),
            )
            reclaimed += 1
    return reclaimed
