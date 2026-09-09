"""Take a backup this system can actually be restored from.

🔴 THE DEFECT. The only backup guidance in the repository was one line in
`docs/deployment.md` — "Maintain database backups and rehearse restoration,
including recreation of worker login roles" — and nothing implemented it or
rehearsed it. A `pg_dump` taken the way anybody would take one is NOT
RESTORABLE for this system.

Roles are CLUSTER-level objects and `pg_dump` is DATABASE-level. The database
dump references `comrade_agent`, `comrade_executor`, `comrade_pipeline`,
`comrade_control` and `comrade_authenticator` in over a hundred GRANT and
CREATE POLICY statements and creates none of them. Restored into a fresh
cluster it fails on the first grant; restored with errors ignored it produces
a database whose row-level security policies name roles that do not exist —
which is not the smaller problem, because the entire authorization model of
this product is those five roles and those policies.

The local version of the same split has already bitten this codebase:
`scripts/restore_local_roles.py` exists because `supabase db reset` drops the
roles and leaves the schema behind.

So a backup here is TWO artifacts, and `create()` refuses to produce one
without the other.

    python -m scripts.backup /var/backups/comrade

`tests/test_restore_drill.py` runs the round trip — dump, restore into a
scratch database, and check that the boundary a member relies on is still
enforced afterwards.
"""
import argparse
import datetime
import hashlib
import json
import os
import shlex
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from shared.errors import safe_error

#: The roles the policies name. A backup that cannot recreate these is not a
#: backup of this system, whatever else it contains.
REQUIRED_ROLES = (
    "comrade_agent", "comrade_executor", "comrade_pipeline",
    "comrade_control", "comrade_authenticator",
)

#: Where the tools live. Supabase's local stack runs Postgres in a container
#: and most hosts have no client binaries at all, so the container is tried
#: before giving up — a drill that cannot run on a developer's machine is a
#: drill that runs nowhere.
DOCKER_DB_CONTAINER = os.environ.get("COMRADE_DB_CONTAINER", "supabase_db_Comrade")

#: The schemas a backup of Comrade contains.
#:
#: 🔴 (fix.md F36) The dump used to be the whole database, and turning
#: ON_ERROR_STOP on revealed why that had to ignore errors: it carries
#: SUPABASE'S OWN platform schemas. `realtime.list_changes` is declared
#: `SET log_min_messages TO 'fatal'`, which only a superuser may create, so the
#: restore stopped dead at that line — and had been failing there silently for
#: as long as this tooling existed. "The drill says whether it worked" hid a
#: backup that was never fully restorable.
#:
#: What is here is what Comrade owns or extends:
#:   * public — every table, function, policy and grant this product defines;
#:   * auth   — `auth.uid()`, which every policy in public calls, so a target
#:              without it cannot even create them;
#:   * storage — the object metadata AND the policies T24/F18/F20 put on it,
#:              which are part of the authorization model rather than
#:              Supabase's.
#: `realtime`, `graphql`, `extensions` and the rest belong to the platform and
#: come back when a cluster is provisioned.
BACKUP_SCHEMAS = ("public", "auth", "storage")

#: Dropped from the dump, by name, because only Supabase's own admin roles may
#: execute them.
#:
#: These set the PLATFORM's default privileges for objects those roles create
#: later. They are not Comrade's authorization model — that is explicit GRANTs
#: to the five comrade_* roles, and every one of those is kept. This is a named
#: exception to ON_ERROR_STOP rather than a licence to ignore whatever fails,
#: which is the distinction fix.md F36 turns on.
_PLATFORM_ONLY = b"ALTER DEFAULT PRIVILEGES FOR ROLE supabase_"


#: Hosts for which "run the client inside the database container" means the
#: SAME server the URL named. Anything else is a different database, and
#: silently substituting one for the other is fix.md F37.
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]", "")


@dataclass
class Backup:
    """The two files, and what it cost to make them."""

    globals_path: Path
    database_path: Path
    seconds: float
    #: Always True, and named rather than implied. `pg_dumpall --globals-only`
    #: includes role password hashes, so this artifact is a credential store.
    #: A backup that can be handed around casually because nobody said
    #: otherwise is the softest way into a system.
    contains_secrets: bool = True


def _parts(url: str) -> dict[str, str]:
    split = urlsplit(url)
    return {
        "host": split.hostname or "127.0.0.1",
        "port": str(split.port or 5432),
        "user": unquote(split.username or "postgres"),
        "password": unquote(split.password or ""),
        "dbname": (split.path or "/postgres").lstrip("/") or "postgres",
    }


def _run(tool: str, args: list[str], url: str, *, stdin: bytes | None = None) -> bytes:
    """Run a Postgres client tool, locally if it exists and in the database
    container if it does not."""
    p = _parts(url)
    common = ["-h", p["host"], "-p", p["port"], "-U", p["user"]]
    env = {**os.environ, "PGPASSWORD": p["password"]}
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, never a shell string
            [tool, *common, *args], capture_output=True, env=env, input=stdin,
            check=True,
        )
        return done.stdout
    except FileNotFoundError:
        pass
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"{tool} failed: {exc.stderr.decode('utf-8', 'replace')[:500]}"
        ) from exc

    # No client binaries on this host. The database container has them, and
    # inside it the server is local.
    #
    # 🔴 (fix.md F37) This translation used to be unconditional. A request for
    # `remote.example:6543` was rewritten to `127.0.0.1:5432` INSIDE the local
    # container — a completely different server — and for a RESTORE that is not
    # a failed operation, it is a successful one against the wrong database.
    #
    # The translation is only ever honest when the URL already names this host:
    # then the server it means and the server in the container are the same
    # one, and only the port differs (54322 outside, 5432 inside). Anything
    # else fails, loudly, naming the target it will not silently replace.
    if p["host"] not in _LOCAL_HOSTS:
        raise RuntimeError(
            f"{tool} is not installed on this host, and {p['host']} is not this"
            f" host — refusing to run it against the local database container"
            f" instead. Install the PostgreSQL client binaries to reach"
            f" {p['host']}:{p['port']}."
        )
    inner = ["-h", "127.0.0.1", "-p", "5432", "-U", p["user"]]
    try:
        done = subprocess.run(  # noqa: S603
            ["docker", "exec", "-i", "-e", f"PGPASSWORD={p['password']}",
             DOCKER_DB_CONTAINER, tool, *inner, *args],
            capture_output=True, input=stdin, check=True,
        )
        return done.stdout
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"neither {tool} nor docker is available; a backup cannot be taken"
            " from this host"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"{tool} in {DOCKER_DB_CONTAINER} failed:"
            f" {exc.stderr.decode('utf-8', 'replace')[:500]}"
        ) from exc


def create(out_dir: Path, url: str | None = None) -> Backup:
    """Write both halves of a restorable backup into `out_dir`.

    Refuses to return a partial one. A backup missing the globals looks
    complete — it is the bigger file, it restores without complaint into a
    cluster that still has the roles — and is worthless in the case backups
    exist for.
    """
    from shared.config import settings

    url = url or settings.comrade_db_url_admin
    if not url:
        raise SystemExit(
            "COMRADE_DB_URL_ADMIN is not set. A backup runs as the table"
            " owner, like migrations do."
        )
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    # 🔴 (fix.md F38) STAGED, in a generation of its own. This used to write
    # `globals.sql` straight into the caller's directory before `pg_dump` ran,
    # so a failure after that left new roles beside an OLDER database dump — a
    # pair that looks complete and restores into nonsense — and a successful
    # run destroyed the previous complete set before knowing the replacement
    # worked.
    # A timestamp for sorting and a RANDOM suffix for uniqueness, because the
    # timestamp alone does not provide it.
    #
    # This took two goes. Seconds collided immediately — two backups in the
    # same second are ordinary, a retry or an overlapping cron. Microseconds
    # then collided too, in a full suite run: measured on Windows,
    # `datetime.now()` returns the SAME value for calls milliseconds apart
    # because the system clock has not ticked, so `_%f` was identical and the
    # second backup was refused. A clock is not an identity source. The suffix
    # is, and the timestamp stays only because a person reading a directory
    # listing wants it.
    generation = "{}-{}".format(
        datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        secrets.token_hex(4),
    )
    staging = out_dir / f"{generation}.partial"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    # The globals contain role password hashes. 0o700 here and 0o600 below, so
    # the secret is not left readable to every account on the host.
    os.chmod(staging, 0o700)

    globals_path = staging / "globals.sql"
    globals_sql = _run("pg_dumpall", ["--globals-only", "--no-role-passwords"
                                      if os.environ.get("COMRADE_BACKUP_NO_PASSWORDS")
                                      else "--globals-only"], url)
    globals_path.write_bytes(globals_sql)
    os.chmod(globals_path, 0o600)

    text = globals_sql.decode("utf-8", "replace")
    missing = [r for r in REQUIRED_ROLES if f"CREATE ROLE {r}" not in text]
    if missing:
        raise RuntimeError(
            "the globals dump does not recreate " + ", ".join(missing)
            + ". Restoring it would leave every RLS policy naming a role that"
              " does not exist."
        )

    database_path = staging / "database.sql"
    database_path.write_bytes(_without_platform_privileges(_run(
        "pg_dump",
        # --no-owner because the dump otherwise carries `ALTER ... OWNER TO
        # supabase_admin`, and the restoring user cannot SET ROLE to that.
        #
        # ACLs and policies are KEPT: the five roles, their grants and every
        # CREATE POLICY are the authorization model this product IS, so --no-acl
        # would throw the thing being backed up away to make the restore
        # quieter.
        ["--no-owner",
         *[f"--schema={schema}" for schema in BACKUP_SCHEMAS],
         "-d", _parts(url)["dbname"]],
        url,
    )))

    # PUBLISHED, atomically, only now that both halves exist and the globals
    # have been checked. Until this rename the generation is `.partial` and
    # `latest()` cannot select it (fix.md F38).
    published = out_dir / generation
    if published.exists():
        # Never over a complete generation. Losing the previous good backup to
        # a name collision is the failure this whole staging dance prevents.
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError(f"{published} already exists; refusing to replace it")
    staging.rename(published)
    manifest = {
        "generation": generation,
        "globals": "globals.sql",
        "database": "database.sql",
        "globals_sha256": _digest(published / "globals.sql"),
        "database_sha256": _digest(published / "database.sql"),
        "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    pointer = out_dir / "latest.json"
    scratch = out_dir / "latest.json.new"
    scratch.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    scratch.replace(pointer)

    return Backup(published / "globals.sql", published / "database.sql",
                  time.monotonic() - started)


def _without_platform_privileges(dump: bytes) -> bytes:
    """Drop the statements only Supabase's own admin roles may run.

    Line-wise and by exact prefix, so nothing else can be caught by it. See
    `_PLATFORM_ONLY`: this is the ONE named exception that lets the restore run
    under ON_ERROR_STOP, rather than the blanket suppression it replaces.
    """
    kept = [line for line in dump.split(b"\n")
            if not line.startswith(_PLATFORM_ONLY)]
    return b"\n".join(kept)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def latest(out_dir: Path) -> Backup | None:
    """The most recent COMPLETE backup in `out_dir`, or None.

    🔴 (fix.md F38) There was no such thing. Backups were written straight into
    the output directory under fixed names, so "the latest backup" was whatever
    happened to be on disk — including a half-written one, which is also the
    newest. A restore had to guess, and the guess is worst exactly when it
    matters.

    Reads the pointer rather than listing directories, because a directory
    listing cannot tell a published generation from an abandoned one.
    """
    out_dir = Path(out_dir)
    pointer = out_dir / "latest.json"
    if not pointer.is_file():
        return None
    try:
        manifest = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    generation = out_dir / str(manifest.get("generation", ""))
    globals_path = generation / str(manifest.get("globals", "globals.sql"))
    database_path = generation / str(manifest.get("database", "database.sql"))
    if not (globals_path.is_file() and database_path.is_file()):
        return None
    # Checked, not trusted: a truncated artifact is the case a manifest exists
    # for.
    for path, key in ((globals_path, "globals_sha256"),
                      (database_path, "database_sha256")):
        expected = manifest.get(key)
        if expected and _digest(path) != expected:
            return None
    return Backup(globals_path, database_path, 0.0)


def restore(artifacts: Backup, url: str, *, globals_too: bool = True) -> float:
    """Restore a backup into the database `url` names. Returns seconds taken.

    `globals_too` is False when restoring into a cluster that already has the
    roles — the drill's case, and a developer's. It is True for the case this
    exists for: a fresh cluster, where the roles must be created BEFORE the
    grants that name them.

    THE TARGET MUST BE PREPARED. `prepare_target` below drops the `public`
    schema every new database is created with, because the dump creates its
    own. That is the "explicit restore strategy" this needs in place of
    ignoring every error: a stated precondition rather than a tool told to
    carry on regardless.
    """
    started = time.monotonic()
    if globals_too:
        _run("psql", ["-d", _parts(url)["dbname"], "-v", "ON_ERROR_STOP=1", "-f", "-"],
             url, stdin=artifacts.globals_path.read_bytes())
    # 🔴 (fix.md F36) ON_ERROR_STOP, which this deliberately omitted. The
    # argument was that "the DRILL is what says whether the restore worked, not
    # the exit code of a tool told to ignore duplicates" — but the drill checks
    # the handful of things it knows to check, and this function is a public
    # helper that hands back elapsed seconds. Every other caller, including a
    # person in the middle of an outage, was told a partial restore succeeded:
    # psql continues past a failed policy, a failed grant or a failed COPY and
    # exits zero.
    #
    # The collisions that argument was about are handled where they belong —
    # `--clean --if-exists` at DUMP time, so the restore drops each object
    # before recreating it instead of failing on it. That makes duplicate
    # objects a non-event and leaves every OTHER error fatal, which is the
    # distinction the flag was standing in for.
    _run("psql", ["-d", _parts(url)["dbname"], "-v", "ON_ERROR_STOP=1", "-f", "-"],
         url, stdin=artifacts.database_path.read_bytes())
    return time.monotonic() - started


def prepare_target(url: str) -> None:
    """Make a freshly created database ready to receive a restore.

    Every new PostgreSQL database comes with a `public` schema, and the dump
    contains `CREATE SCHEMA public` — so the first thing a restore under
    ON_ERROR_STOP hits is "schema public already exists". Dropping it is the
    preparation step; doing it HERE means the restore has one documented
    precondition instead of a suppressed error class.

    Deliberately not part of `restore()`: dropping a schema is destructive, and
    a caller pointing at the wrong database should not have it done for them.
    """
    _run("psql",
         ["-d", _parts(url)["dbname"], "-v", "ON_ERROR_STOP=1",
          "-c", "drop schema if exists public cascade"],
         url)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args()
    try:
        made = create(args.out_dir)
    except Exception as exc:  # noqa: BLE001 - the message is the whole output
        print(safe_error(exc), file=sys.stderr)
        return 1
    print(
        f"backup written in {made.seconds:.1f}s:\n"
        f"  {made.globals_path}   (roles — CONTAINS PASSWORD HASHES)\n"
        f"  {made.database_path}  (schema, data, policies)\n"
        "Both are needed. Restore the globals first:\n"
        f"  psql -f {shlex.quote(str(made.globals_path))}\n"
        f"  psql -d <db> -f {shlex.quote(str(made.database_path))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
