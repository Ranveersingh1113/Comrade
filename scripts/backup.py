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
import os
import shlex
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

    globals_path = out_dir / "globals.sql"
    globals_sql = _run("pg_dumpall", ["--globals-only", "--no-role-passwords"
                                      if os.environ.get("COMRADE_BACKUP_NO_PASSWORDS")
                                      else "--globals-only"], url)
    globals_path.write_bytes(globals_sql)

    text = globals_sql.decode("utf-8", "replace")
    missing = [r for r in REQUIRED_ROLES if f"CREATE ROLE {r}" not in text]
    if missing:
        raise RuntimeError(
            "the globals dump does not recreate " + ", ".join(missing)
            + ". Restoring it would leave every RLS policy naming a role that"
              " does not exist."
        )

    database_path = out_dir / "database.sql"
    database_path.write_bytes(_run("pg_dump", ["-d", _parts(url)["dbname"]], url))

    return Backup(globals_path, database_path, time.monotonic() - started)


def restore(artifacts: Backup, url: str, *, globals_too: bool = True) -> float:
    """Restore a backup into the database `url` names. Returns seconds taken.

    `globals_too` is False when restoring into a cluster that already has the
    roles — the drill's case, and a developer's. It is True for the case this
    exists for: a fresh cluster, where the roles must be created BEFORE the
    grants that name them.
    """
    started = time.monotonic()
    if globals_too:
        _run("psql", ["-d", _parts(url)["dbname"], "-v", "ON_ERROR_STOP=1", "-f", "-"],
             url, stdin=artifacts.globals_path.read_bytes())
    # Not ON_ERROR_STOP: a dump of a Supabase database re-creates extensions
    # and system roles the target already has, and those collisions are noise.
    # The DRILL is what says whether the restore worked, not the exit code of
    # a tool told to ignore duplicates.
    _run("psql", ["-d", _parts(url)["dbname"], "-f", "-"], url,
         stdin=artifacts.database_path.read_bytes())
    return time.monotonic() - started


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
