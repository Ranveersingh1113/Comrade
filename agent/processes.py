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
    MOUNT, SANDBOX_UID, _SECURITY_FLAGS, _git_mask,
)
from shared.config import settings
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

#: The network preview containers join, and the ONLY way anything reaches them.
#:
#: 🔴 This replaces `--network bridge` plus `-p 127.0.0.1::<port>`, and both
#: were wrong. Publishing a host port puts an unauthenticated development
#: server on a port of the host — and in the deployed topology it does not even
#: work, because the API runs in its own container, so the host's loopback is
#: not the API's. The proxy could not reach what it was meant to proxy.
#:
#: An `--internal` user-defined network solves both. Nothing outside it can
#: reach a preview, the containers have no default external route, and the API
#: joins the same network and dials containers by name through Docker's DNS.
PREVIEW_NETWORK = "comrade-preview"


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


def _ensure_network() -> None:
    """Create the preview network if it is absent. Idempotent.

    `--internal` is the load-bearing flag: it gives the network no gateway to
    the outside, so a preview container cannot reach the internet even though
    it is on a bridge. Dependencies were installed in a separate phase that had
    the network; running does not need one.
    """
    try:
        _docker(["docker", "network", "inspect", PREVIEW_NETWORK])
        return
    except ProcessError:
        pass
    try:
        _docker(["docker", "network", "create", "--internal", PREVIEW_NETWORK])
    except ProcessError as exc:
        # Another worker may have created it between the inspect and here.
        if "already exists" not in str(exc):
            raise


def _run_argv(name: str, root: Path, command: str, port: int | None) -> list[str]:
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
        "--network", PREVIEW_NETWORK,
        "--read-only", "--tmpfs", "/tmp",
        *_git_mask(root),
        *_SECURITY_FLAGS,
        "--user", f"{SANDBOX_UID}:{SANDBOX_UID}",
        "-v", f"{root}:{MOUNT}",
        "-w", MOUNT,
        settings.comrade_sandbox_image,
        "sh", "-lc", command,
    ]


def start(
    team_id: str, thread_id: str, command: str, *,
    root: Path, port: int | None = None, agent_run_id: str | None = None,
) -> dict:
    """Start a long-running process for this thread and record it.

    `root` is the thread's working tree, resolved by the CALLER — the same way
    repo_run takes it from `_root(tool_context)`. This module supervises
    containers; deciding which checkout a thread owns is the repo layer's job,
    and doing it here would make every supervision test need a git repository.
    """
    if port is not None and not (MIN_PORT <= port <= MAX_PORT):
        raise ProcessError(
            f"{port} cannot be previewed. Use a port between {MIN_PORT} and"
            f" {MAX_PORT}; below {MIN_PORT} is privileged."
        )
    if not command.strip():
        raise ProcessError("no command given")

    # Written FIRST. See the module header.
    with team_session(Role.AGENT, team_id) as conn:
        row = conn.execute(
            "insert into public.sandbox_processes"
            " (team_id, thread_id, agent_run_id, command, port)"
            " values (%s,%s,%s,%s,%s) returning id",
            (team_id, thread_id, agent_run_id, command, port),
        ).fetchone()
    process_id = str(row[0])

    name = f"comrade-proc-{uuid.uuid4().hex}"
    try:
        _ensure_network()
        container_id = _docker(_run_argv(name, root, command, port))
    except ProcessError as exc:
        _finish(team_id, process_id, "failed", detail=str(exc))
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
            "select container_id, state from public.sandbox_processes"
            " where id=%s and team_id=%s",
            (process_id, team_id),
        ).fetchone()
    if row is None:
        raise ProcessError("no such process in this thread.")
    container_id, state = row
    if state not in ("starting", "running"):
        return {"id": process_id, "state": state}
    if container_id:
        _kill(container_id)
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
            "select id, container_id from public.sandbox_processes"
            " where state in ('starting','running')"
            "   and last_seen_at < now() - make_interval(hours => %s)",
            (IDLE_HOURS,),
        ).fetchall()
        for process_id, container_id in rows:
            if container_id:
                _kill(container_id)
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
