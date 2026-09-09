"""What the settlement migrations do to runs that already exist.

🔴 THE HARNESS DEFECT these replace (fix.md F51). The first version of these
tests dropped `usage_owner` and `usage_checkpoint_at` from `public.agent_runs`
in the CONFIGURED database and re-applied the migrations to put them back. That
restores the definitions and destroys the values — for every run in the
database, not just the seeded team's — and an interruption partway leaves the
schema without the columns at all. It ran in the ordinary gate, not a reset
lane.

A test that has to break the schema to make its point needs a schema it is
allowed to break. These run against a disposable copy, restored from a real
backup of the real database, and the developer's own database is only read.

What is still real here: the migration bodies are the committed files, executed
as written, and the settlement is `shared.usage.settle_cancelled` itself against
a database whose schema came out of `pg_dump`. What is NOT covered, and is
covered by tests/test_run_cancellation.py on the shared database instead, is the
HTTP route — nothing here needs DDL to exercise that, so nothing here does.
"""
import psycopg
import pytest

from scripts import backup
from shared.config import settings
from shared.db import Role, team_session
from shared.usage import settle_cancelled
from tests._seed import A1, TEAM_A, cleanup, seed

UPGRADE_DB = "comrade_settlement_upgrade"

OWNERSHIP = "supabase/migrations/20260909130000_settlement_ownership.sql"
CHECKPOINT = "supabase/migrations/20260909160000_settlement_checkpoint.sql"

#: A run in the shared database that must come through all of this untouched.
#: The point of the finding is that the values, not just the columns, were being
#: destroyed — so what is checked is a value.
SENTINEL_OWNER = "sentinel-worker"


def _admin(url: str | None = None):
    conn = psycopg.connect(url or settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _sql(path: str) -> str:
    from pathlib import Path
    return (Path(__file__).parents[1] / path).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def upgrade_db(tmp_path_factory):
    """A disposable copy of the real database, rolled back to pre-upgrade.

    Restored from an actual backup rather than built from the migration chain:
    a fresh database has none of Supabase's platform schema, so applying the
    chain into one fails long before it reaches these two migrations. The dump
    carries the whole schema, and the roles the policies name are cluster-level
    and already exist.
    """
    out = tmp_path_factory.mktemp("upgrade-backup")

    # Seeded BEFORE the dump, the way the restore drill does: the copy can only
    # contain what the snapshot contained, and these tests need a team, a
    # member and a thread to hang a run on.
    conn = _admin()
    try:
        with conn.cursor() as cur:
            cleanup(cur)
            seed(cur)
    finally:
        conn.close()

    artifacts = backup.create(out)

    conn = _admin()
    try:
        conn.execute(f'drop database if exists "{UPGRADE_DB}" with (force)')
        conn.execute(f'create database "{UPGRADE_DB}"')
    finally:
        conn.close()

    url = settings.comrade_db_url_admin.rsplit("/", 1)[0] + "/" + UPGRADE_DB
    try:
        backup.prepare_target(url)
        backup.restore(artifacts, url, globals_too=False)

        # Back to the schema as it stood before the two settlement migrations.
        # Destructive, and that is fine: this database exists to be destroyed.
        conn = _admin(url)
        try:
            conn.execute("drop trigger if exists keep_usage_owner"
                         " on public.agent_runs")
            conn.execute("drop trigger if exists expire_usage_checkpoint"
                         " on public.agent_runs")
            conn.execute("alter table public.agent_runs"
                         " drop column if exists usage_owner,"
                         " drop column if exists usage_checkpoint_at")
        finally:
            conn.close()
        yield url
    finally:
        conn = _admin()
        try:
            conn.execute(f'drop database if exists "{UPGRADE_DB}" with (force)')
        finally:
            conn.close()


def _legacy_run(url: str, *, attempts: int, reserved: int = 6000) -> str:
    """A run as it exists in the schema BEFORE the settlement migrations.

    Written while the columns genuinely do not exist, so its NULLs afterwards
    are the migration's rather than the test's — which is the whole question.
    """
    conn = _admin(url)
    try:
        thread_id = conn.execute(
            "select id from public.threads where team_id=%s and title='General'",
            (TEAM_A,)).fetchone()[0]
        run_id = str(conn.execute(
            "insert into public.agent_runs (team_id, requester_id, thread_id,"
            " trigger_type, input_summary, status, attempts, worker_id,"
            " tokens_reserved, usage_bucket, input_tokens, output_tokens,"
            " finished_at)"
            " values (%s,%s,%s,'user','legacy','cancelled',%s,null,%s,"
            "         date_trunc('hour', now()), 0, 0, now()) returning id",
            (TEAM_A, A1, thread_id, attempts, reserved)).fetchone()[0])
        conn.execute("delete from public.usage_buckets where team_id=%s", (TEAM_A,))
        conn.execute(
            "insert into public.usage_buckets (team_id, bucket, turns, tokens)"
            " values (%s, date_trunc('hour', now()), 1, %s)", (TEAM_A, reserved))
        return run_id
    finally:
        conn.close()


def _upgrade(url: str) -> None:
    conn = _admin(url)
    try:
        conn.execute(_sql(OWNERSHIP))
        conn.execute(_sql(CHECKPOINT))
    finally:
        conn.close()


def _bucket(url: str) -> int:
    conn = _admin(url)
    try:
        row = conn.execute(
            "select tokens from public.usage_buckets where team_id=%s"
            " and bucket=date_trunc('hour', now())", (TEAM_A,)).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def _settle(url: str, run_id: str) -> None:
    """The real settlement, against the upgraded copy.

    `settle_cancelled` takes the caller's connection — it has to, so that
    cancellation and settlement can share a transaction — and that is what lets
    this run the committed function against a database other than the
    configured one.
    """
    with psycopg.connect(url) as conn:
        settle_cancelled(conn, TEAM_A, run_id)
        conn.commit()


def test_a_legacy_executed_run_is_not_settled_as_if_it_never_ran(upgrade_db):
    """🔴 fix.md F50, on a database the migrations really upgraded.

    A run that executed and lost its lease before these migrations has
    attempts > 0 and, afterwards, NULL in both new columns — because they are
    deliberately unbackfilled. That NULL owner means "nobody has written this
    column yet", and reading it as "nothing ever executed" released the whole
    reservation of a run that really had run.
    """
    run_id = _legacy_run(upgrade_db, attempts=1)

    _upgrade(upgrade_db)

    conn = _admin(upgrade_db)
    try:
        shape = conn.execute(
            "select attempts, worker_id, usage_owner, usage_checkpoint_at"
            " from public.agent_runs where id=%s", (run_id,)).fetchone()
    finally:
        conn.close()
    assert shape == (1, None, None, None), shape

    _settle(upgrade_db, run_id)

    conn = _admin(upgrade_db)
    try:
        finalized = conn.execute(
            "select usage_finalized_at from public.agent_runs where id=%s",
            (run_id,)).fetchone()[0]
    finally:
        conn.close()
    assert finalized is None, (
        "an unbackfilled legacy row was read as proof nothing ever executed"
    )
    assert _bucket(upgrade_db) == 6000


def test_a_legacy_run_that_was_never_claimed_is_still_refunded(upgrade_db):
    """The other half, and why the evidence is `attempts`. A run nothing ever
    claimed really did spend nothing, and its estimate must still go back —
    including for rows written before either column existed."""
    run_id = _legacy_run(upgrade_db, attempts=0)

    _upgrade(upgrade_db)

    _settle(upgrade_db, run_id)

    assert _bucket(upgrade_db) == 0


# ---------------------------------------------------------------------------
# F51 — and the developer's own database is untouched by all of that
# ---------------------------------------------------------------------------

@pytest.fixture
def sentinel(seeded):
    """A run in the CONFIGURED database carrying settlement metadata.

    Its survival is the finding: dropping a column and re-adding it restores
    the definition and loses every value, which no amount of schema comparison
    would have caught.
    """
    conn = _admin()
    try:
        thread_id = conn.execute(
            "select id from public.threads where team_id=%s and title='General'",
            (TEAM_A,)).fetchone()[0]
        run_id = str(conn.execute(
            "insert into public.agent_runs (team_id, requester_id, thread_id,"
            " trigger_type, input_summary, status, attempts, usage_owner,"
            " usage_checkpoint_at, tokens_reserved)"
            " values (%s,%s,%s,'user','sentinel','cancelled',2,%s,now(),4321)"
            " returning id", (TEAM_A, A1, thread_id, SENTINEL_OWNER)).fetchone()[0])
        yield run_id
    finally:
        conn.close()


def _sentinel_state(run_id: str) -> tuple:
    conn = _admin()
    try:
        return conn.execute(
            "select usage_owner, usage_checkpoint_at is not null, attempts"
            " from public.agent_runs where id=%s", (run_id,)).fetchone()
    finally:
        conn.close()


def _settlement_schema() -> list:
    conn = _admin()
    try:
        columns = conn.execute(
            "select column_name, data_type from information_schema.columns"
            " where table_schema='public' and table_name='agent_runs'"
            "   and column_name in ('usage_owner','usage_checkpoint_at')"
            " order by column_name").fetchall()
        triggers = conn.execute(
            "select tgname from pg_trigger"
            " where tgrelid='public.agent_runs'::regclass and not tgisinternal"
            " order by tgname").fetchall()
        return [columns, triggers]
    finally:
        conn.close()


def test_the_upgrade_tests_leave_the_developers_database_alone(
    sentinel, upgrade_db,
):
    """🔴 THE HARNESS DEFECT (fix.md F51). The previous fixture dropped both
    columns from the configured database. Re-applying the migrations put the
    definitions back and left every value NULL — permanently, for every run,
    not only the seeded team's — and it ran in the ordinary gate.

    So: a sentinel with real settlement metadata, the whole upgrade exercise
    against the disposable copy, and the sentinel unchanged afterwards. Schema
    AND values, because restoring the schema is exactly what made the old
    fixture look harmless.
    """
    before_schema = _settlement_schema()
    before_row = _sentinel_state(sentinel)
    assert before_row == (SENTINEL_OWNER, True, 2), before_row

    run_id = _legacy_run(upgrade_db, attempts=1)
    _upgrade(upgrade_db)
    _settle(upgrade_db, run_id)

    assert _sentinel_state(sentinel) == before_row
    assert _settlement_schema() == before_schema


def test_a_failing_upgrade_test_still_leaves_the_database_alone(
    sentinel, upgrade_db,
):
    """And when the exercise fails partway. The old fixture's worst case was an
    interruption between the drop and the re-apply, which left the configured
    database with no settlement columns at all; nothing here can reach it."""
    before_schema = _settlement_schema()
    before_row = _sentinel_state(sentinel)

    with pytest.raises(psycopg.Error):
        run_id = _legacy_run(upgrade_db, attempts=1)
        _upgrade(upgrade_db)
        # Fails inside the disposable database, after its schema has been
        # changed and while a settlement is in flight.
        with psycopg.connect(upgrade_db) as conn:
            settle_cancelled(conn, TEAM_A, run_id)
            conn.execute("select 1 from public.no_such_table")

    assert _sentinel_state(sentinel) == before_row
    assert _settlement_schema() == before_schema


def test_the_configured_database_is_never_the_disposable_one(upgrade_db):
    """Cheap, and it is the assertion that would have caught the original
    fixture: the database these tests break is not the one anything else uses."""
    assert UPGRADE_DB in upgrade_db
    assert upgrade_db != settings.comrade_db_url_admin
    assert not settings.comrade_db_url_admin.endswith("/" + UPGRADE_DB)

    # And the settlement path under test really is reachable as the app role on
    # the configured database, so these tests are about the same function the
    # product calls rather than an admin-only variant of it.
    with team_session(Role.AGENT, TEAM_A) as conn:
        settle_cancelled(conn, TEAM_A, "00000000-0000-0000-0000-000000000000")
