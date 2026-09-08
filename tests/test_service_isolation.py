"""What each service is given, and what it is refused.

🔴 THE DEFECTS.

1. The CROSS-TEAM control role could read `jobs.payload`, and
   `enqueue_github_event` queues the whole parsed webhook body. A private
   repository's pull request descriptions, commit messages and review comments
   sat in a column readable by the one role that is deliberately not scoped to
   any team. T21 moved DOCUMENT bytes out of the payload for exactly this
   reason and left every other payload where it was.

2. An unset role URL was not "unconfigured", it was "connect with libpq
   defaults" — `team_session` handed the empty string to a pool, which reaches
   for PGHOST/PGUSER, a .pgpass, or peer auth on the local socket. On a host
   where peer auth works that is a superuser session with no row security,
   arrived at by forgetting a variable.

3. The table-owner URL was a REQUIRED setting, so every API and worker process
   had to carry the RLS-bypassing credential in its environment just to boot.
"""
import subprocess
import sys

import psycopg
import pytest

from shared.config import settings
from shared.db import Role, connect, team_session
from tests._seed import TEAM_A


def _control():
    conn = psycopg.connect(settings.comrade_control_db_url)
    conn.autocommit = True
    return conn


# ---------------------------------------------------------------------------
# What the queue exposes to the control plane
# ---------------------------------------------------------------------------

def test_the_control_plane_cannot_read_a_queued_payload(seeded):
    """🔴 It could, and `ingest_github` queues the whole webhook body."""
    with _control() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("select payload from public.jobs").fetchone()


def test_the_control_plane_still_sees_the_queue_it_has_to_run(seeded):
    """The point is to withhold the CONTENT, not to break the sweeper."""
    with _control() as conn:
        conn.execute(
            "select id, team_id, job_type, status, attempts, lease_expires_at"
            " from public.jobs limit 1"
        ).fetchall()


def test_a_private_repository_body_never_reaches_the_control_role(seeded):
    """The concrete leak, end to end: a pull request description from a
    private repository, queued, then read back by the cross-team role."""
    from pipeline.github import enqueue_github_event

    enqueue_github_event(
        TEAM_A, "pull_request", "isolation-1",
        {"pull_request": {
            "body": "internal: rotate the prod signing key before Friday",
            "number": 3,
        }},
    )

    with _control() as conn:
        # Not by name and not by `select *` either — a revoked column makes
        # the star expansion itself fail, which is the point.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("select payload::text from public.jobs").fetchall()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("select * from public.jobs").fetchall()

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        found = conn.execute(
            "select payload->'body'->>'pull_request' is not null"
            " from public.jobs where dedupe_key='isolation-1'"
        ).fetchone()
    assert found is not None, "the handler's own role must still see it"


def test_the_handler_still_receives_its_payload(seeded):
    """Withholding it from the control role must not withhold it from the
    handler — the job is useless without it."""
    from pipeline import worker
    from pipeline.github import enqueue_github_event

    enqueue_github_event(
        TEAM_A, "pull_request", "isolation-2", {"pull_request": {"number": 7}},
    )
    seen: list[dict] = []
    handlers = {
        job_type: (lambda team_id, payload: seen.append(payload))
        for job_type in worker._HANDLERS
    }

    # Other tests leave work on the queue and the claim takes the oldest.
    while worker.run_once(handlers=handlers, worker_id="isolation-test"):
        if any(p.get("event") == "pull_request" for p in seen):
            break

    assert any(p.get("event") == "pull_request" for p in seen),         "the handler must still be given the payload the control role cannot read"


# ---------------------------------------------------------------------------
# Missing configuration
# ---------------------------------------------------------------------------

def test_an_unset_role_url_refuses_rather_than_guessing(monkeypatch):
    """🔴 An empty conninfo is not "no connection". libpq fills it in from
    PGHOST/PGUSER, a .pgpass, or peer auth on the local socket — so a
    forgotten variable could become a superuser session with no row
    security."""
    import shared.db as db

    monkeypatch.setitem(db._URLS, Role.CONTROL, "")
    with pytest.raises(RuntimeError, match="COMRADE_CONTROL_DB_URL"):
        with team_session(Role.CONTROL, TEAM_A):
            pass


def test_an_unset_role_url_is_refused_on_the_bare_connect_path(monkeypatch):
    import shared.db as db

    monkeypatch.setitem(db._URLS, Role.PIPELINE, "")
    with pytest.raises(RuntimeError, match="COMRADE_PIPELINE_DB_URL"):
        with connect(Role.PIPELINE):
            pass


# ---------------------------------------------------------------------------
# The table owner
# ---------------------------------------------------------------------------

def test_a_service_starts_without_the_table_owner_credential():
    """🔴 `comrade_db_url_admin` was required, so every API and worker process
    had to carry the RLS-bypassing credential just to import its settings."""
    code = (
        "import os;"
        " os.environ.pop('COMRADE_DB_URL_ADMIN', None);"
        " from shared.config import Settings;"
        " s = Settings(_env_file=None, comrade_agent_db_url='x',"
        "   comrade_executor_db_url='x', comrade_pipeline_db_url='x');"
        " print(repr(s.comrade_db_url_admin))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "''"


def test_the_table_owner_is_refused_unless_a_process_asks_for_it(monkeypatch):
    """Having the credential in the environment is not the same as being a
    process that may use it. The API and both workers never declare this, so
    the table owner is unreachable from them even on a host where the variable
    is present."""
    import shared.db as db

    monkeypatch.setattr(db, "_ADMIN_ALLOWED", False)
    with pytest.raises(RuntimeError, match="table owner"):
        with connect(Role.ADMIN):
            pass


def test_the_migration_entrypoint_is_such_a_process():
    import shared.migrations as migrations

    assert migrations.allow_table_owner is not None


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------

def _rotate(role_name: str, password: str) -> None:
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        # ALTER ROLE ... PASSWORD takes a literal, not a parameter. Both
        # values here are test constants, never user input.
        conn.execute(f"alter role {role_name} password '{password}'")
    finally:
        conn.close()


def test_a_rotated_credential_fails_loudly_instead_of_falling_back(seeded):
    """What must not happen during a key rotation is a QUIET degradation.

    A process still holding the old secret has to stop working and say so. If
    a stale credential could reach the database under any other role — the
    table owner most of all — a rotation would silently widen access instead
    of narrowing it, and nothing would report a problem.

    Rotated on a throwaway role so a failure part-way through cannot leave the
    suite's real roles locked out.
    """
    import shared.db as db

    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        conn.execute("drop role if exists comrade_rotation_probe")
        conn.execute(
            "create role comrade_rotation_probe login password 'first-secret'"
        )
        conn.execute("grant usage on schema public to comrade_rotation_probe")
        dsn = psycopg.conninfo.make_conninfo(
            settings.comrade_db_url_admin,
            user="comrade_rotation_probe", password="first-secret",
        )
        psycopg.connect(dsn).close()          # the old secret works

        _rotate("comrade_rotation_probe", "second-secret")
        db.close_pools()

        monkey = dict(db._URLS)
        db._URLS[Role.PIPELINE] = dsn
        try:
            with pytest.raises(psycopg.OperationalError):
                with connect(Role.PIPELINE) as c:
                    c.execute("select 1")
        finally:
            db._URLS.clear()
            db._URLS.update(monkey)
            db.close_pools()
    finally:
        conn.execute("revoke usage on schema public from comrade_rotation_probe")
        conn.execute("drop role if exists comrade_rotation_probe")
        conn.close()


def test_the_control_plane_sees_what_a_job_is_about_not_what_it_carries(seeded):
    """`subject` exists so two cross-team sweeps can find a `sync_repo` job's
    repository without being handed the payload. It is the job's IDENTITY and
    must stay that way — a repository name is already readable by this role
    through `github_repos`, so it exposes nothing new, which is the test for
    whether something belongs in this column."""
    from pipeline.github import enqueue_github_event
    from pipeline.repo_sync import enqueue_sync

    secret = "internal: the staging password is in the PR body"
    enqueue_github_event(
        TEAM_A, "pull_request", "subject-1",
        {"pull_request": {"body": secret, "number": 9}},
    )
    enqueue_sync(TEAM_A, "acme/app")

    with _control() as conn:
        subjects = [
            r[0] for r in conn.execute(
                "select subject from public.jobs where team_id=%s", (TEAM_A,)
            ).fetchall()
        ]

    assert "acme/app" in subjects, "the sweeps need this"
    assert all(s is None or len(s) < 200 for s in subjects), subjects
    assert not any(s and secret in s for s in subjects)
