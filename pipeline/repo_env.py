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

from pipeline.repo_deps import (
    environment_key, has_lockfile, install, manifest_for,
)
from pipeline.worker import PermanentJobError, register
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
