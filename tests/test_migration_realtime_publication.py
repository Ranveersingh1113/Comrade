"""The frontend's Realtime subscriptions need their tables published.

Published is necessary and NOT sufficient. Realtime evaluates each table's RLS
SELECT policy against the row as it appears in the WAL, so with the default
replica identity the WAL carries only the primary key, the policy has no
team_id to test, and every row is dropped — the subscription connects, the
INSERT succeeds, and nothing ever arrives.

That is the same silent failure the publication migration was written to fix,
one layer down, and it is why the two are asserted together here. Adding a
table to the publication without setting replica identity full re-creates it.
"""
import psycopg
import pytest

from shared.config import settings

# Every table a screen subscribes to via postgres_changes.
PUBLISHED = [
    "messages", "tasks", "milestones", "consent_queue", "documents",
    "memory_compilations",
]


@pytest.fixture(scope="module")
def conn():
    c = psycopg.connect(settings.comrade_db_url_admin)
    c.autocommit = True
    yield c
    c.close()


def _published(conn) -> set[str]:
    rows = conn.execute(
        "select tablename from pg_publication_tables"
        " where pubname='supabase_realtime' and schemaname='public'"
    ).fetchall()
    return {r[0] for r in rows}


@pytest.mark.parametrize("table", PUBLISHED)
def test_table_is_published_to_realtime(conn, table):
    assert table in _published(conn), (
        f"{table} is not in the supabase_realtime publication;"
        " subscriptions to it will connect and then never fire"
    )


def test_memory_facts_are_not_published(conn):
    """Facts reach the UI through the diff card, not a raw firehose.

    memory_versions churns on every compile; the room only needs to know a
    compilation happened. Keeping it unpublished is deliberate.
    """
    assert "memory_versions" not in _published(conn)


def test_jobs_are_not_published(conn):
    """`jobs` is the backend queue — members have no RLS access to it at all."""
    assert "jobs" not in _published(conn)


@pytest.mark.parametrize("table", PUBLISHED)
def test_published_table_carries_its_whole_row(conn, table):
    """🔴 Every published table was 'd' until 20260901110000.

    Realtime cannot apply an RLS policy to a row it can only see the primary
    key of, so it delivered nothing at all — for the group room, the task
    board, the consent badge, the document list and the memory diff card. It
    reads as latency rather than breakage, since every screen also refetches
    on focus, which is why it survived a publication test that only checked
    the table was listed.
    """
    ident = conn.execute(
        "select relreplident from pg_class c"
        " join pg_namespace n on n.oid = c.relnamespace"
        " where n.nspname='public' and c.relname=%s",
        (table,),
    ).fetchone()[0]
    assert ident == "f", (
        f"public.{table} has replica identity '{ident}', not 'f' — Realtime"
        " will filter every row out and the subscription will stay silent"
    )
