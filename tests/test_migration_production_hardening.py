"""Schema assertions for queue recovery and citation-integrity hardening."""
import psycopg
import pytest

from shared.config import settings
from tests._seed import B2, TEAM_A, TEAM_B, VER_A


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def test_jobs_have_lease_and_dedupe_columns(admin):
    columns = {
        row[0] for row in admin.execute(
            "select column_name from information_schema.columns"
            " where table_schema='public' and table_name='jobs'"
        ).fetchall()
    }
    assert {"lease_expires_at", "dedupe_key"} <= columns


def test_page_titles_are_case_insensitive_per_team(admin, seeded):
    admin.execute(
        "insert into public.memory_pages (team_id, title) values (%s,'Deadlines')",
        (TEAM_A,),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        admin.execute(
            "insert into public.memory_pages (team_id, title) values (%s,'deadlines')",
            (TEAM_A,),
        )
    admin.execute(
        "insert into public.memory_pages (team_id, title) values (%s,'deadlines')",
        (TEAM_B,),
    )


def test_citation_cannot_reference_another_teams_message(admin, seeded):
    message_id = admin.execute(
        "insert into public.messages (team_id, thread_type, sender_kind, sender_id, body)"
        " values (%s,'group','user',%s,'other team') returning id",
        (TEAM_B, B2),
    ).fetchone()[0]
    with pytest.raises(psycopg.errors.RaiseException, match="version team"):
        admin.execute(
            "insert into public.memory_citations (version_id, source_kind, source_id)"
            " values (%s,'message',%s)",
            (VER_A, message_id),
        )
