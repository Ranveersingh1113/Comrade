"""Schema assertions for the compiler-v2 migration."""
import psycopg
import pytest

from shared.config import settings
from tests._seed import TEAM_A


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def test_change_type_accepts_invalidated(admin, seeded):
    entry_id = admin.execute(
        "insert into public.memory_entries (team_id) values (%s) returning id",
        (TEAM_A,),
    ).fetchone()[0]
    row = admin.execute(
        "insert into public.memory_versions (entry_id, team_id, fact, change_type,"
        " is_active, valid_until) values (%s,%s,'dropped','invalidated',false,now())"
        " returning id",
        (entry_id, TEAM_A),
    ).fetchone()
    assert row is not None


def test_change_type_still_rejects_unknown(admin, seeded):
    entry_id = admin.execute(
        "insert into public.memory_entries (team_id) values (%s) returning id",
        (TEAM_A,),
    ).fetchone()[0]
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute(
            "insert into public.memory_versions (entry_id, team_id, fact, change_type)"
            " values (%s,%s,'x','bogus')",
            (entry_id, TEAM_A),
        )


def test_vector_columns_removed(admin):
    """2026-07-15 pivot: retrieval-style memory deleted — columns must be gone."""
    cols = admin.execute(
        "select column_name from information_schema.columns"
        " where table_schema='public' and table_name='memory_versions'"
        " and column_name in ('embedding','embedding_model','embedding_dim')"
    ).fetchall()
    assert cols == []
