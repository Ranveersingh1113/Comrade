"""An environment a team asked for, whose state they can see.

Installing a repository's dependencies runs its build hooks with network
access. That is not implied by connecting a repository, so it is a thing a
team turns on per repository — and once on, a thing they can watch, because an
environment that silently is not there turns every red test suite into a
mystery.

THE FIVE STATES, AND WHY ONLY THREE ARE STORED
------------------------------------------------
    disabled   env_enabled is false. Nothing is built or run.
    none       enabled, never built. The reconciler will pick it up.
    building   a job is installing right now.
    ready      built, and built from what the checkout says today.
    failed     the install did not work; env_error says why.
    stale      built, but from a different key than the checkout would
               produce now.

`disabled` and `stale` are DERIVED, not columns. Disabled is
`env_enabled = false`; stale is `env_key <> environment_key(...)`. Storing
either would be a second copy of something already known and free to drift
from it — the same reason `last_cloned_at` is a column and "is this checked
out" is not.

WHY A RECONCILER RATHER THAN AN ENDPOINT
------------------------------------------
Enabling is a row write the frontend makes straight to Supabase under RLS,
where au_github_repos_update already requires team leadership. There is no
server-side event to hang a build on, and a reconciler converges whether the
row changed through the UI, through psql, or while the worker was down — the
same argument sweep_orphan_workspaces makes.

This is automatic in the way the earlier version was NOT: it fires only for a
repository whose leader has explicitly turned it on.
"""
import logging
import subprocess

from pipeline.repo_deps import (
    environment_key, has_lockfile, install, manifest_for,
)
from pipeline.worker import PermanentJobError, register
from shared.config import settings
from shared.db import Role, connect, team_session
from shared.workspace import deps_volume, repo_checkout

logger = logging.getLogger(__name__)

#: Per tick, so enabling twenty repositories at once does not queue twenty
#: installs ahead of every other job.
BUILD_BATCH = 5


def _row(conn, team_id: str, repo_full_name: str):
    return conn.execute(
        "select env_enabled, env_status, env_key, env_error"
        "  from public.github_repos"
        " where team_id = %s and repo_full_name = %s",
        (team_id, repo_full_name),
    ).fetchone()


def status_for(team_id: str, repo_full_name: str, requester_id: str) -> dict:
    """What a member — or the agent acting for one — should be told.

    Read as the MEMBER, the same as connected_repo: a turn acts on behalf of
    whoever asked, and if they cannot see the repository then neither can the
    turn.

    The `detail` is written for a person, because the agent repeats it. "I have
    no environment for this repository" and "your tests failed" are different
    sentences, and being unable to tell them apart is the failure this whole
    feature exists to prevent.
    """
    from shared.db import user_session

    with user_session(requester_id) as conn:
        row = _row(conn, team_id, repo_full_name)
    if row is None:
        return {"status": "unknown",
                "detail": "that repository is not connected to this team."}

    enabled, stored, key, error = row
    if not enabled:
        return {
            "status": "disabled",
            "detail": (
                "no dependency environment is set up for this repository, so"
                " only the standard library and the tools already in the image"
                " are available. A team lead can turn one on in project setup."
            ),
        }
    if stored is None:
        return {"status": "none",
                "detail": "the environment has been requested and not built yet."}
    if stored == "building":
        return {"status": "building",
                "detail": "the environment is being built right now."}
    if stored == "failed":
        return {"status": "failed",
                "detail": f"the environment could not be built: {error or 'no detail'}"}

    root = repo_checkout(team_id, repo_full_name)
    manifest = manifest_for(root) if root.exists() else None
    if manifest is None:
        return {"status": "stale",
                "detail": "the manifest this environment was built from is gone."}
    if key != environment_key(root, manifest):
        return {
            "status": "stale",
            "detail": (
                "the environment is out of date with the current code and is"
                " being rebuilt. Results from it may not reflect recent changes."
            ),
        }
    return {"status": "ready", "detail": "the environment is ready."}


def _set_status(team_id: str, repo_full_name: str, status: str,
                *, key: str | None = None, error: str | None = None) -> None:
    """Report what happened. The pipeline holds a COLUMN grant here and cannot
    touch env_enabled — it says what the environment is doing, never whether
    the team wanted one."""
    with team_session(Role.PIPELINE, team_id) as conn:
        conn.execute(
            "update public.github_repos"
            "   set env_status = %s, env_key = %s, env_error = %s,"
            "       env_updated_at = now()"
            " where team_id = %s and repo_full_name = %s",
            (status, key, error, team_id, repo_full_name),
        )


def enqueue_build(team_id: str, repo_full_name: str) -> str | None:
    """Queue one environment build. Deduped while one is already pending."""
    from psycopg.types.json import Json

    with team_session(Role.PIPELINE, team_id) as conn:
        row = conn.execute(
            "insert into public.jobs (team_id, job_type, payload, dedupe_key)"
            " values (%s,'build_environment',%s,%s)"
            " on conflict (team_id, job_type, dedupe_key)"
            " where dedupe_key is not null and status in ('pending','processing')"
            " do nothing returning id",
            (team_id, Json({"repo_full_name": repo_full_name}),
             f"env:{repo_full_name}"),
        ).fetchone()
    return str(row[0]) if row else None


def handle_build_environment(team_id: str, payload: dict) -> None:
    """Install this repository's dependencies, and record what happened.

    Never raises for a dependency that will not resolve. That is a fact about
    the project, and burning three worker attempts to rediscover it delays the
    only thing that helps: telling the team what pip said.
    """
    name = payload.get("repo_full_name")
    if not name:
        raise PermanentJobError("build_environment job carries no repo_full_name")

    with connect(Role.ADMIN) as conn:
        row = _row(conn, team_id, name)
    if row is None or not row[0]:
        # Turned off between queuing and running. Not an error: the answer to
        # "should this be built" is read at BUILD time, not at queue time, so a
        # member who changes their mind is obeyed rather than raced.
        logger.info("environment build skipped for %s: not enabled", name)
        return

    root = repo_checkout(team_id, name)
    manifest = manifest_for(root) if root.exists() else None
    if manifest is None:
        _set_status(team_id, name, "failed",
                    error="no requirements.txt or pyproject.toml in this repository")
        return

    key = environment_key(root, manifest)
    _set_status(team_id, name, "building", key=None)

    outcome = install(team_id, name)
    if outcome["status"] in ("installed", "current"):
        lock = has_lockfile(root)
        logger.info("environment ready for %s from %s%s", name, manifest,
                    f" (pinned by {lock})" if lock else " (no lockfile)")
        _set_status(team_id, name, "ready", key=key)
        return

    _set_status(
        team_id, name, "failed",
        error=str(outcome.get("detail") or outcome["status"])[:2000],
    )


register("build_environment", handle_build_environment)


def sweep_environments() -> list[str]:
    """Queue builds for environments a team asked for and does not have.

    Runs from worker.tick(). It fires ONLY for repositories whose leader set
    env_enabled — which is the entire difference between this and the version
    that installed a manifest the moment a repository was connected.
    """
    with connect(Role.ADMIN) as conn:
        rows = conn.execute(
            "select team_id, repo_full_name, env_status, env_key"
            "  from public.github_repos"
            " where env_enabled and last_cloned_at is not null"
            "   and (env_status is null or env_status = 'ready')"
            " order by env_updated_at nulls first limit %s",
            (BUILD_BATCH,),
        ).fetchall()

    queued: list[str] = []
    for team_id, name, status, key in rows:
        if status == "ready":
            # Only rebuild a ready environment when it has actually gone stale.
            root = repo_checkout(str(team_id), name)
            manifest = manifest_for(root) if root.exists() else None
            if manifest is None or key == environment_key(root, manifest):
                continue
        try:
            if enqueue_build(str(team_id), name):
                queued.append(f"{team_id}/{name}")
        except Exception:  # noqa: BLE001 - one bad row must not stop the rest
            logger.exception("could not queue an environment build for %s", name)
    if queued:
        logger.info("queued %d environment build(s)", len(queued))
    return queued


#: Docker reports volume sizes as human strings ("1.234GB"), SI units, from
#: go-units. Parsed rather than measured with a container per volume, which
#: would be one container start each on every tick.
_UNITS = {"B": 1, "KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12}


def _bytes_from(text: str) -> int:
    """Docker's human size as bytes, or 0 if it cannot be read.

    ZERO ON FAILURE, and the direction is the point: an unparseable size makes
    the total look SMALLER, so nothing is evicted. Guessing high would delete a
    team's environment because a format changed.
    """
    value = (text or "").strip().upper().replace("IB", "B")
    for unit in ("TB", "GB", "MB", "KB", "B"):
        if value.endswith(unit):
            try:
                return int(float(value[: -len(unit)].strip()) * _UNITS[unit])
            except ValueError:
                return 0
    return 0


def _environment_volumes() -> dict[str, int]:
    """Every dependency volume and its size, in one call."""
    import json

    try:
        out = subprocess.run(  # noqa: S603 - fixed argv
            ["docker", "system", "df", "-v", "--format", "{{json .Volumes}}"],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if out.returncode != 0:
        return {}
    try:
        volumes = json.loads(out.stdout or "[]") or []
    except ValueError:
        return {}
    return {
        v["Name"]: _bytes_from(v.get("Size", ""))
        for v in volumes
        if str(v.get("Name", "")).startswith("comrade-deps-")
    }


def enforce_env_disk_cap() -> list[str]:
    """Evict least-recently-built environments until the total is under budget.

    Availability, not tidiness: these volumes sit on the same disk as Postgres,
    and a `torch` or a `node_modules` is gigabytes. Eviction is safe in the way
    it is for checkouts — a volume is rebuildable from the manifest, and the
    reconciler rebuilds it if the team still wants one.

    🔴 EVICTING CLEARS env_status. A volume removed while the row still says
    'ready' is the database asserting something that is not true — the agent
    would mount an environment that no longer exists, and the member would be
    told their tests failed rather than that their environment was reclaimed.
    Every other silent-success bug on this branch had this shape.

    Least-recently-BUILT, not least-recently-used: Docker does not track access
    time on a volume, and env_updated_at is the honest approximation. Said
    plainly rather than described as LRU, which it is not.
    """
    budget = int(settings.comrade_env_max_gb * 1024**3)
    sizes = _environment_volumes()
    total = sum(sizes.values())
    if not sizes or total <= budget:
        return []

    with connect(Role.ADMIN) as conn:
        rows = conn.execute(
            "select team_id, repo_full_name, env_updated_at"
            "  from public.github_repos"
            " where env_status is not null"
            " order by env_updated_at nulls first"
        ).fetchall()

    evicted: list[str] = []
    for team_id, name, _updated in rows:
        if total <= budget:
            break
        volume = deps_volume(str(team_id), name)
        size = sizes.get(volume)
        if size is None:
            continue
        removed = subprocess.run(  # noqa: S603
            ["docker", "volume", "rm", "-f", volume],
            capture_output=True, timeout=120,
        ).returncode == 0
        if not removed:
            # In use by a running container, most likely. Say so; a silent
            # skip here is how a disk fills while a sweep reports success.
            logger.warning("could not evict environment volume %s", volume)
            continue
        _set_status(str(team_id), name, None, key=None,
                    error="the environment was reclaimed for disk space and"
                          " will be rebuilt")
        total -= size
        evicted.append(f"{team_id}/{name}")

    if evicted:
        logger.info("evicted %d environment(s) to stay under %.1fGB",
                    len(evicted), settings.comrade_env_max_gb)
    return evicted
