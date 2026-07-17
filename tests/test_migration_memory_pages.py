"""Schema + RLS assertions for the memory_pages migration."""
import psycopg
import pytest

from shared.config import settings
from tests._seed import A1, B1, TEAM_A, TEAM_B, as_user


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def test_pages_table_and_entries_page_id_exist(admin):
    cols = {
        r[0]
        for r in admin.execute(
            "select column_name from information_schema.columns"
            " where table_schema='public' and table_name='memory_pages'"
        ).fetchall()
    }
    assert {"id", "team_id", "title", "description"} <= cols
    entry_cols = {
        r[0]
        for r in admin.execute(
            "select column_name from information_schema.columns"
            " where table_schema='public' and table_name='memory_entries'"
            " and column_name='page_id'"
        ).fetchall()
    }
    assert entry_cols == {"page_id"}


def test_page_title_unique_per_team_only(admin, seeded):
    admin.execute(
        "insert into public.memory_pages (team_id, title) values (%s,'Deadlines')",
        (TEAM_A,),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        admin.execute(
            "insert into public.memory_pages (team_id, title) values (%s,'Deadlines')",
            (TEAM_A,),
        )
    # same title on another team is fine
    row = admin.execute(
        "insert into public.memory_pages (team_id, title) values (%s,'Deadlines')"
        " returning id",
        (TEAM_B,),
    ).fetchone()
    assert row is not None


def test_members_read_only_own_team_pages(admin, seeded):
    admin.execute(
        "insert into public.memory_pages (team_id, title) values (%s,'Decisions')",
        (TEAM_A,),
    )
    with as_user(A1) as conn:
        titles = [r[0] for r in conn.execute(
            "select title from public.memory_pages"
        ).fetchall()]
    assert "Decisions" in titles
    with as_user(B1) as conn:
        titles = [r[0] for r in conn.execute(
            "select title from public.memory_pages"
        ).fetchall()]
    assert "Decisions" not in titles


def test_members_cannot_write_pages(admin, seeded):
    with as_user(A1) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "insert into public.memory_pages (team_id, title) values (%s,'Hack')",
                (TEAM_A,),
            )
