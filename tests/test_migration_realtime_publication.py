"""The frontend's Realtime subscriptions need their tables published."""
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
