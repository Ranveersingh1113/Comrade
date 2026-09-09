"""Actually restoring a backup, rather than believing in one.

🔴 THE DEFECT. The only backup guidance in the repository was one line in
`docs/deployment.md`: "Maintain database backups and rehearse restoration,
including recreation of worker login roles." Nothing implemented it and
nothing rehearsed it — and a `pg_dump` taken the way anybody would take one is
NOT RESTORABLE for this system.

Roles are cluster-level objects. A database dump references
`comrade_agent`, `comrade_executor`, `comrade_pipeline`, `comrade_control` and
`authenticated` in over a hundred GRANT and CREATE POLICY statements and
creates none of them. Restored into a fresh cluster it fails on the first
grant; restored with errors ignored it produces a database whose row-level
security policies name roles that do not exist — which is not a smaller
problem, because the entire authorization model of this product is those five
roles and those policies.

The codebase has already been bitten by the local version of this:
`scripts/restore_local_roles.py` exists because `supabase db reset` drops the
roles and leaves the schema. The production version of the same split is what
this drill exists to catch.
"""
import json
import re
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).parents[1]

from scripts import backup
from shared.config import settings
from tests._seed import A1, A2, B1, TEAM_A, TEAM_B, cleanup, seed

#: The roles the policies name. Every one of these must come back or the
#: restored database is a database with no authorization model.
REQUIRED_ROLES = (
    "comrade_agent", "comrade_executor", "comrade_pipeline",
    "comrade_control", "comrade_authenticator",
)

DRILL_DB = "comrade_restore_drill"


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


@pytest.fixture(scope="module")
def drill(tmp_path_factory):
    """Take a backup, restore it into a scratch database, hand back its URL.

    Module-scoped: the round trip is the expensive part, and every assertion
    below is a read against the same restored copy.
    """
    out = tmp_path_factory.mktemp("backup")

    # Seed BEFORE the dump. 🔴 Without this the snapshot has no rows, and the
    # isolation checks below pass on an empty table — which is exactly the
    # shape of a restore drill that proves nothing. The module-scoped fixture
    # runs before any test's own `seeded`, so it has to arrange its own data.
    conn = _admin()
    try:
        with conn.cursor() as cur:
            cleanup(cur)
            seed(cur)
        # The seed makes only team-visible discussions, so the drill arranges
        # the second boundary itself: a thread inside team A that only A1 may
        # open. A restore has to bring back BOTH — membership and visibility.
        thread_id = conn.execute(
            "insert into public.threads (team_id, title, visibility, kind,"
            " created_by) values (%s,'Security review','restricted',"
            " 'discussion',%s) returning id", (TEAM_A, A1),
        ).fetchone()[0]
        conn.execute(
            "insert into public.thread_participants (thread_id, team_id,"
            " user_id, added_by) values (%s,%s,%s,%s)",
            (thread_id, TEAM_A, A1, A1),
        )
    finally:
        conn.close()

    artifacts = backup.create(out)

    conn = _admin()
    try:
        conn.execute(f'drop database if exists "{DRILL_DB}" with (force)')
        conn.execute(f'create database "{DRILL_DB}"')
    finally:
        conn.close()

    url = settings.comrade_db_url_admin.rsplit("/", 1)[0] + "/" + DRILL_DB
    try:
        # 🔴 (fix.md F36) PREPARED first. The restore now runs under
        # ON_ERROR_STOP, and every new database arrives with a `public` schema
        # the dump also creates — so the precondition is explicit rather than
        # an error class the restore was told to ignore.
        backup.prepare_target(url)
        restore_seconds = backup.restore(artifacts, url, globals_too=False)
        yield artifacts, url, restore_seconds
    finally:
        conn = _admin()
        try:
            conn.execute(f'drop database if exists "{DRILL_DB}" with (force)')
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# What the backup has to contain
# ---------------------------------------------------------------------------

def test_the_backup_includes_the_roles_the_policies_name(drill):
    """🔴 A `pg_dump` does not. This is the whole finding: the dump REFERENCES
    these roles over a hundred times and CREATES none of them, so a restore
    into a fresh cluster has no authorization model at all."""
    artifacts, _, _ = drill
    globals_sql = artifacts.globals_path.read_text(encoding="utf-8", errors="replace")

    missing = [r for r in REQUIRED_ROLES if f"CREATE ROLE {r}" not in globals_sql]
    assert not missing, f"the backup cannot recreate: {', '.join(missing)}"


def test_the_database_dump_alone_would_not_be_enough(drill):
    """Stated as a test so nobody 'simplifies' the backup down to one file."""
    artifacts, _, _ = drill
    database_sql = artifacts.database_path.read_text(
        encoding="utf-8", errors="replace")

    assert "comrade_agent" in database_sql, "the policies reference the roles"
    assert "CREATE ROLE comrade_agent" not in database_sql, (
        "if pg_dump starts including roles this drill needs rethinking, not"
        " deleting"
    )


def test_the_backup_says_it_carries_secrets(drill):
    """`pg_dumpall --globals-only` includes role password hashes. An artifact
    that can be handed around casually because nobody said otherwise is how a
    backup becomes the softest way in."""
    artifacts, _, _ = drill

    assert artifacts.contains_secrets is True


# ---------------------------------------------------------------------------
def test_the_applied_migration_ledger_comes_back(drill):
    """🔴 (fix.md F36 follow-up) Without this the restored database cannot say
    which migrations it has. `server/app.py`'s readiness check and
    `shared/migrations.py` both read `supabase_migrations.schema_migrations`,
    so a target missing it is one that either fails readiness or re-applies
    every migration over objects that are already there."""
    _artifacts, url, _seconds = drill
    conn = psycopg.connect(url)
    conn.autocommit = True
    try:
        restored = {
            row[0] for row in conn.execute(
                "select version from supabase_migrations.schema_migrations"
            ).fetchall()
        }
    finally:
        conn.close()

    on_disk = {
        path.stem.partition("_")[0]
        for path in (ROOT / "supabase" / "migrations").glob("*.sql")
    }

    assert restored, "the restored database has no migration ledger at all"
    missing = sorted(on_disk - restored)
    assert not missing, f"the restore lost these applied versions: {missing}"


# What survives the restore
# ---------------------------------------------------------------------------

def test_the_schema_comes_back(drill):
    _, url, _ = drill
    with psycopg.connect(url) as conn:
        tables = {
            row[0] for row in conn.execute(
                "select table_name from information_schema.tables"
                " where table_schema='public'"
            ).fetchall()
        }
    for expected in ("threads", "messages", "consent_queue", "jobs",
                     "memory_pages", "documents", "agent_runs"):
        assert expected in tables, f"{expected} did not survive the restore"


def test_row_level_security_is_still_enabled(drill):
    """A restore that brings back the tables and not the `enable row level
    security` is a database that silently answers every query."""
    _, url, _ = drill
    with psycopg.connect(url) as conn:
        unprotected = [
            row[0] for row in conn.execute(
                "select c.relname from pg_class c"
                " join pg_namespace n on n.oid = c.relnamespace"
                " where n.nspname='public' and c.relkind='r'"
                "   and c.relrowsecurity = false"
                "   and c.relname in ('threads','messages','consent_queue',"
                "                     'memory_pages','documents','agent_runs')"
            ).fetchall()
        ]
    assert not unprotected, f"RLS off after restore on: {unprotected}"


def test_the_policies_come_back(drill):
    _, url, _ = drill
    with psycopg.connect(url) as conn:
        policies = conn.execute(
            "select count(*) from pg_policies where schemaname='public'"
        ).fetchone()[0]
    assert policies > 20, f"only {policies} policies survived"


def _as(url: str, user_id: str, sql: str, params=None):
    with psycopg.connect(url) as conn:
        conn.execute("set role authenticated")
        conn.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (json.dumps({"sub": user_id, "role": "authenticated"}),),
        )
        return conn.execute(sql, params).fetchone()[0]


def test_a_member_still_sees_only_their_own_team(drill, seeded):
    """The property the whole restore exists to preserve. Not "are the rows
    there" — anyone can check that — but "is the boundary still enforced".

    Both directions, because a restored database with an empty `threads` table
    would satisfy the denial half on its own and prove nothing.
    """
    _, url, _ = drill

    owner_sees = _as(url, A1,
                     "select count(*) from public.threads where team_id=%s",
                     (TEAM_A,))
    stranger_sees = _as(url, B1,
                        "select count(*) from public.threads where team_id=%s",
                        (TEAM_A,))

    assert owner_sees > 0, (
        "the restored database has no threads for this team, so the denial"
        " below would pass on an empty table"
    )
    assert stranger_sees == 0, "a restored database handed one team another's"


def test_a_restored_restricted_thread_stays_restricted(drill, seeded):
    """Membership is one boundary; thread visibility is a second one inside
    it, and a restore has to bring back both. A2 is in the same team and not a
    participant, which is the case a team-level check cannot see."""
    _, url, _ = drill
    sql = ("select count(*) from public.threads"
           " where team_id=%s and visibility='restricted'")

    participant = _as(url, A1, sql, (TEAM_A,))
    teammate = _as(url, A2, sql, (TEAM_A,))

    assert participant > 0, (
        "no restricted thread restored; the denial below would be vacuous"
    )
    assert teammate == 0, "a teammate could read a restored restricted thread"


def test_consent_state_survives(drill, seeded):
    """An approval is a record of a decision somebody made. Losing it in a
    restore is losing the audit, and re-approving is not the same thing."""
    _, url, _ = drill
    with psycopg.connect(url) as conn:
        columns = {
            row[0] for row in conn.execute(
                "select column_name from information_schema.columns"
                " where table_name='consent_queue'"
            ).fetchall()
        }
    for expected in ("status", "tool_name", "action_hash", "tier",
                     "requesting_member_id", "resolved_at", "resolution_reason"):
        assert expected in columns, f"consent_queue lost {expected}"


def test_the_drill_is_timed(drill, capsys):
    """An RTO nobody has measured is a wish.

    Both halves are recorded and printed, because the number an operator needs
    during an incident is the RESTORE time — the backup's duration only tells
    you how long the nightly job takes.
    """
    artifacts, _, restore_seconds = drill

    assert artifacts.seconds > 0, "the backup was not timed"
    assert restore_seconds > 0, "the restore was not timed"
    with capsys.disabled():
        print(f"\n  [drill] backup {artifacts.seconds:.1f}s /"
              f" restore {restore_seconds:.1f}s"
              f" ({artifacts.database_path.stat().st_size / 1024**2:.1f} MB)")


def test_a_member_can_still_use_the_restored_database(drill):
    """"Usable user journeys after restoration" — not just that the boundary
    holds, but that somebody inside it can still do the thing.

    A restore that brings back locked-down tables nobody can read is a
    different kind of failure from one that leaks, and a drill that only
    checks denial would call it a success.
    """
    _, url, _ = drill

    threads = _as(url, A1,
                  "select count(*) from public.threads where team_id=%s",
                  (TEAM_A,))
    messages = _as(url, A1,
                   "select count(*) from public.messages where team_id=%s",
                   (TEAM_A,))
    teams = _as(url, A1, "select count(*) from public.teams where id=%s",
                (TEAM_A,))

    assert teams == 1, "a member cannot see their own team after the restore"
    assert threads > 0, "a member cannot see any thread after the restore"
    assert messages > 0, "a member cannot read any message after the restore"


# ---------------------------------------------------------------------------
# The thing a backup cannot save
# ---------------------------------------------------------------------------

def test_uncommitted_workspace_work_is_not_claimed_to_be_recoverable():
    """🔴 The plan's words: "Do not call dirty worktrees rebuildable."

    A checkout IS re-clonable and that is what makes eviction safe — but only
    while it is clean. `enforce_disk_cap` already refuses to evict a workspace
    with uncommitted work for exactly this reason, and the recovery
    documentation has to say the same thing rather than implying a backup
    covers it.
    """
    from pathlib import Path

    runbook = (Path(__file__).resolve().parent.parent
               / "docs" / "operations.md").read_text(encoding="utf-8")

    assert "uncommitted" in runbook.lower(), (
        "the recovery section never mentions the one thing no backup restores"
    )


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

#: A rollback hazard is something an OLDER image relied on being taken away.
#:
#: Deliberately narrow. `revoke all on function ... from public, anon,
#: authenticated` sitting next to a `create function` is hardening applied at
#: creation, not a hazard — a guard that flagged those would flag 33 of this
#: repository's migrations and be deleted within a week.
_REVOKE_FROM_WORKER = re.compile(r"revoke\s+[^;]*?\s+from\s+[^;]*comrade_",
                                 re.IGNORECASE | re.DOTALL)
_DROPS = re.compile(r"drop\s+(column|table)\b", re.IGNORECASE)


def test_every_narrowing_migration_is_named_in_the_rollback_register():
    """🔴 "No automatic downgrade of irreversible schema" is only a rule if
    something enforces it.

    Migrations here are expand-then-contract, so the previous image runs
    against the migrated schema and a rollback is "deploy the old commit". A
    migration that REMOVES or NARROWS something breaks that, and the operator
    finds out during an incident. `20260908130000_queue_payload_privacy.sql`
    is the worked example: it revokes `select (payload)` from the control role,
    and the worker before that release selects `payload` in its claim.

    This forbids nothing. It requires that the register in
    `docs/operations.md` name the migration, so the reversal is written down
    before it is needed rather than reconstructed under pressure.
    """
    root = Path(__file__).resolve().parent.parent
    runbook = (root / "docs" / "operations.md").read_text(encoding="utf-8")

    undocumented = [
        path.name
        for path in sorted((root / "supabase" / "migrations").glob("*.sql"))
        for body in [path.read_text(encoding="utf-8")]
        if (_REVOKE_FROM_WORKER.search(body) or _DROPS.search(body))
        and path.name not in runbook
    ]

    assert not undocumented, (
        "these migrations narrow something an older image may rely on, and the"
        " rollback register in docs/operations.md does not list them: "
        + ", ".join(undocumented)
    )
