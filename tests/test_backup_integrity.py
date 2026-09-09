"""Three ways the backup tooling could hand back something worthless.

🔴 F36 — A PARTIAL RESTORE REPORTED SUCCESS. The database half was restored
without `ON_ERROR_STOP`, deliberately, on the argument that "the DRILL is what
says whether the restore worked, not the exit code of a tool told to ignore
duplicates". The drill checks a handful of things it happens to know about.
`restore()` is a public helper that returns elapsed seconds, and every other
caller — a person recovering an outage included — got a number that meant
nothing. psql happily continues past a failed policy or a failed COPY and exits
zero.

🔴 F37 — A REMOTE RESTORE COULD HIT THE WRONG SERVER. When host client binaries
are missing, `_run` falls back to running the tool inside the local database
container, and rewrites the connection to `127.0.0.1:5432` — the container's own
server. A backup or restore requested for `remote.example:6543` silently
targeted a completely different database. For a RESTORE that is not a failed
operation, it is a successful one against the wrong thing.

🔴 F38 — A FAILED BACKUP DESTROYED THE LAST GOOD ONE. `globals.sql` was written
before `pg_dump` ran, into the caller's output directory. A failure after that
left new globals beside an older database dump — a pair that looks complete and
is not — and re-running into the same directory overwrote the previous complete
set before knowing whether the replacement would work.
"""
import datetime
import os
import subprocess
from pathlib import Path

import pytest

from scripts import backup


# ---------------------------------------------------------------------------
# F37 — the requested target
# ---------------------------------------------------------------------------

def test_a_remote_target_is_never_translated_to_the_local_container(monkeypatch):
    """🔴 The finding's own measurement: requested `remote.example:6543`,
    fallback selected `127.0.0.1:5432`."""
    calls: list[list[str]] = []

    def _no_local_binaries(argv, **kwargs):
        calls.append(argv)
        if argv[0] != "docker":
            raise FileNotFoundError(2, argv[0])
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", _no_local_binaries)

    with pytest.raises(RuntimeError) as refused:
        backup._run("pg_dump", ["-d", "postgres"],
                    "postgresql://u:p@remote.example:6543/postgres")

    assert "remote.example" in str(refused.value)
    assert not any(argv[0] == "docker" for argv in calls), (
        "a remote request was sent to the local database container"
    )


def test_a_local_target_may_still_use_the_container(monkeypatch):
    """The fallback exists because a developer's host has no client binaries
    and Supabase runs Postgres in a container. That case is legitimate: the
    server the URL names and the server in the container are the same one."""
    seen: list[list[str]] = []

    def _only_docker(argv, **kwargs):
        seen.append(argv)
        if argv[0] != "docker":
            raise FileNotFoundError(2, argv[0])
        return subprocess.CompletedProcess(argv, 0, b"dump", b"")

    monkeypatch.setattr(subprocess, "run", _only_docker)

    out = backup._run("pg_dump", ["-d", "postgres"],
                      "postgresql://u:p@127.0.0.1:54322/postgres")

    assert out == b"dump"
    assert any(argv[0] == "docker" for argv in seen)


def test_the_container_fallback_keeps_the_database_and_user(monkeypatch):
    """Translating the endpoint must not quietly translate anything else."""
    seen: list[list[str]] = []

    def _only_docker(argv, **kwargs):
        seen.append(argv)
        if argv[0] != "docker":
            raise FileNotFoundError(2, argv[0])
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", _only_docker)
    backup._run("pg_dump", ["-d", "comrade"],
                "postgresql://owner:p@localhost:54322/comrade")

    docker_call = next(argv for argv in seen if argv[0] == "docker")
    assert "-U" in docker_call
    assert docker_call[docker_call.index("-U") + 1] == "owner"
    assert "comrade" in docker_call


# ---------------------------------------------------------------------------
# F36 — a restore that failed says so
# ---------------------------------------------------------------------------

def test_the_dump_carries_only_what_this_product_owns(monkeypatch, tmp_path):
    """🔴 What ON_ERROR_STOP exposed. The dump was the whole database, and it
    carries Supabase's own platform schemas — `realtime.list_changes` is
    declared `SET log_min_messages TO 'fatal'`, which only a superuser may
    create. The restore had been stopping there for as long as this tooling
    existed, invisibly, because every error was ignored."""
    monkeypatch.setattr("shared.config.settings.comrade_db_url_admin",
                        "postgresql://u:p@127.0.0.1:54322/postgres")
    asked: list[list[str]] = []

    def _run(tool, args, url, stdin=None):
        asked.append([tool, *args])
        if tool == "pg_dumpall":
            return "".join(f"CREATE ROLE {r};\n"
                           for r in backup.REQUIRED_ROLES).encode()
        return b"-- schema\n"

    monkeypatch.setattr(backup, "_run", _run)
    backup.create(tmp_path)

    dump = next(call for call in asked if call[0] == "pg_dump")
    assert "--schema=public" in dump
    assert "--schema=auth" in dump, "every policy in public calls auth.uid()"
    assert "--schema=storage" in dump, "T24/F18/F20 put policies there"
    assert not any(a.startswith("--schema=realtime") for a in dump)
    # Ownership statements name supabase_admin, which the restoring user
    # cannot SET ROLE to.
    assert "--no-owner" in dump
    # And the model itself is NOT stripped to make the restore quieter.
    assert "--no-acl" not in dump


def test_platform_default_privileges_are_dropped_by_name(monkeypatch, tmp_path):
    """The one named exception that lets ON_ERROR_STOP stay on. Narrow, and by
    exact prefix, so it cannot quietly swallow one of Comrade's own grants."""
    dump = (
        b"GRANT SELECT ON public.teams TO comrade_agent;\n"
        b"ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public"
        b" GRANT ALL ON TABLES TO anon;\n"
        b"CREATE POLICY p ON public.teams FOR SELECT TO comrade_agent"
        b" USING (true);\n"
    )

    kept = backup._without_platform_privileges(dump)

    assert b"comrade_agent" in kept
    assert b"CREATE POLICY" in kept
    assert b"supabase_admin" not in kept


def test_the_database_restore_stops_on_a_sql_error(monkeypatch, tmp_path):
    """🔴 psql continues past a failed policy or COPY and exits zero. A helper
    that returns elapsed seconds for that is telling the caller a partial
    restore worked."""
    invocations: list[list[str]] = []

    def _record(tool, args, url, stdin=None):
        invocations.append([tool, *args])
        return b""

    monkeypatch.setattr(backup, "_run", _record)
    artifacts = backup.Backup(tmp_path / "globals.sql", tmp_path / "database.sql", 0.0)
    artifacts.globals_path.write_bytes(b"-- roles\n")
    artifacts.database_path.write_bytes(b"-- schema\n")

    backup.restore(artifacts, "postgresql://u:p@127.0.0.1:54322/postgres")

    database_call = invocations[-1]
    assert "ON_ERROR_STOP=1" in database_call, (
        "the database half was restored with errors ignored"
    )


def test_a_failing_statement_makes_restore_raise(monkeypatch, tmp_path):
    """The behaviour, not just the flag: a psql that exits non-zero must not
    come back as a duration."""
    def _boom(tool, args, url, stdin=None):
        if "ON_ERROR_STOP=1" in args and stdin and b"schema" in stdin:
            raise RuntimeError("psql failed: ERROR: policy already exists")
        return b""

    monkeypatch.setattr(backup, "_run", _boom)
    artifacts = backup.Backup(tmp_path / "globals.sql", tmp_path / "database.sql", 0.0)
    artifacts.globals_path.write_bytes(b"-- roles\n")
    artifacts.database_path.write_bytes(b"-- schema\n")

    with pytest.raises(RuntimeError):
        backup.restore(artifacts, "postgresql://u:p@127.0.0.1:54322/postgres")


# ---------------------------------------------------------------------------
# F38 — publishing a complete set
# ---------------------------------------------------------------------------

def _fake_dumps(monkeypatch, *, fail_on: str | None = None):
    """pg_dumpall and pg_dump, without a database."""
    roles = "".join(f"CREATE ROLE {r};\n" for r in backup.REQUIRED_ROLES)

    def _run(tool, args, url, stdin=None):
        if tool == fail_on:
            raise RuntimeError(f"{tool} failed: disk full")
        if tool == "pg_dumpall":
            return roles.encode()
        return b"-- schema and data\n"

    monkeypatch.setattr(backup, "_run", _run)


def test_a_failure_during_the_database_dump_leaves_no_new_pair(
    monkeypatch, tmp_path,
):
    """🔴 globals.sql was written first, so this left new roles beside
    whatever database dump was already there — a pair that looks complete."""
    monkeypatch.setattr("shared.config.settings.comrade_db_url_admin",
                        "postgresql://u:p@127.0.0.1:54322/postgres")
    _fake_dumps(monkeypatch)
    first = backup.create(tmp_path)
    original = first.database_path.read_bytes()

    _fake_dumps(monkeypatch, fail_on="pg_dump")
    with pytest.raises(RuntimeError):
        backup.create(tmp_path)

    assert first.globals_path.exists()
    assert first.database_path.read_bytes() == original, (
        "the previous complete backup was overwritten by a failed one"
    )


def test_the_previous_generation_survives_a_successful_one(monkeypatch, tmp_path):
    """"The previous complete backup remains selectable." Overwriting in place
    means one bad dump and one bad restore attempt is all it takes."""
    monkeypatch.setattr("shared.config.settings.comrade_db_url_admin",
                        "postgresql://u:p@127.0.0.1:54322/postgres")
    _fake_dumps(monkeypatch)
    first = backup.create(tmp_path)
    second = backup.create(tmp_path)

    assert first.globals_path.exists()
    assert first.database_path.exists()
    assert second.globals_path != first.globals_path


def test_two_backups_are_distinct_even_on_a_clock_that_does_not_tick(
    monkeypatch, tmp_path,
):
    """The generation name must be unique BY CONSTRUCTION, not by hoping time
    passed between two calls.

    This is pinned with a frozen clock because the real thing was found the
    hard way, twice. Second resolution collided immediately. Microseconds then
    collided in a full-suite run: on Windows `datetime.now()` returns the same
    value for calls milliseconds apart, because the system clock has not
    ticked, so the second backup was refused as "already exists". A clock is
    not an identity source.
    """
    monkeypatch.setattr("shared.config.settings.comrade_db_url_admin",
                        "postgresql://u:p@127.0.0.1:54322/postgres")
    _fake_dumps(monkeypatch)

    frozen = datetime.datetime(2026, 9, 9, 12, 0, 0, 500,
                               tzinfo=datetime.timezone.utc)

    class _Stopped(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(backup.datetime, "datetime", _Stopped)

    first = backup.create(tmp_path)
    second = backup.create(tmp_path)

    assert first.globals_path.parent != second.globals_path.parent
    assert first.database_path.exists()
    assert second.database_path.exists()


def test_a_completion_manifest_names_the_published_generation(
    monkeypatch, tmp_path,
):
    """Something has to say WHICH generation is complete, or a restore has to
    guess from directory listings — and a half-written one is newest."""
    monkeypatch.setattr("shared.config.settings.comrade_db_url_admin",
                        "postgresql://u:p@127.0.0.1:54322/postgres")
    _fake_dumps(monkeypatch)
    made = backup.create(tmp_path)

    latest = backup.latest(tmp_path)

    assert latest is not None
    assert latest.globals_path == made.globals_path
    assert latest.database_path == made.database_path


def test_a_failed_backup_does_not_become_the_latest(monkeypatch, tmp_path):
    monkeypatch.setattr("shared.config.settings.comrade_db_url_admin",
                        "postgresql://u:p@127.0.0.1:54322/postgres")
    _fake_dumps(monkeypatch)
    good = backup.create(tmp_path)

    _fake_dumps(monkeypatch, fail_on="pg_dump")
    with pytest.raises(RuntimeError):
        backup.create(tmp_path)

    assert backup.latest(tmp_path).database_path == good.database_path


def test_a_failure_before_the_globals_are_valid_publishes_nothing(
    monkeypatch, tmp_path,
):
    """The roles check already refused a globals dump that cannot recreate the
    five roles. It must refuse before anything is published, not after."""
    monkeypatch.setattr("shared.config.settings.comrade_db_url_admin",
                        "postgresql://u:p@127.0.0.1:54322/postgres")

    def _thin(tool, args, url, stdin=None):
        return b"-- no roles here\n"

    monkeypatch.setattr(backup, "_run", _thin)

    with pytest.raises(RuntimeError):
        backup.create(tmp_path)

    assert backup.latest(tmp_path) is None


@pytest.mark.skipif(os.name == "nt",
                    reason="POSIX file modes; Windows ACLs are a separate story")
def test_the_role_passwords_are_not_world_readable(monkeypatch, tmp_path):
    """globals.sql contains password hashes and says so in the CLI output."""
    monkeypatch.setattr("shared.config.settings.comrade_db_url_admin",
                        "postgresql://u:p@127.0.0.1:54322/postgres")
    _fake_dumps(monkeypatch)
    made = backup.create(tmp_path)

    assert made.globals_path.stat().st_mode & 0o077 == 0
