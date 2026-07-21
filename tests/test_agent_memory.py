"""The agent's view of the team wiki (Claude Code's auto-loaded memory)."""
import psycopg

from agent.agent import wiki_section
from shared.config import settings
from tests._seed import TEAM_A, TEAM_B


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _seed_page(cur, team_id, title, fact, description=""):
    page_id = cur.execute(
        "insert into public.memory_pages (team_id, title, description)"
        " values (%s,%s,%s) returning id",
        (team_id, title, description),
    ).fetchone()[0]
    entry_id = cur.execute(
        "insert into public.memory_entries (team_id, page_id) values (%s,%s)"
        " returning id",
        (team_id, page_id),
    ).fetchone()[0]
    version_id = cur.execute(
        "insert into public.memory_versions (entry_id, team_id, fact, change_type)"
        " values (%s,%s,%s,'added') returning id",
        (entry_id, team_id, fact),
    ).fetchone()[0]
    return page_id, entry_id, version_id


def test_index_lists_page_titles_and_descriptions(seeded):
    conn = _admin()
    try:
        with conn.cursor() as cur:
            _seed_page(cur, TEAM_A, "Deadlines", "Demo is Friday",
                       description="key dates")
    finally:
        conn.close()

    section = wiki_section(TEAM_A)
    assert "Deadlines" in section
    assert "key dates" in section
    # the index is titles only — never the facts themselves
    assert "Demo is Friday" not in section


def test_index_omits_pages_with_no_active_facts(seeded):
    conn = _admin()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "insert into public.memory_pages (team_id, title) values (%s,'Hollow')",
                (TEAM_A,),
            )
    finally:
        conn.close()
    assert "Hollow" not in wiki_section(TEAM_A)


def test_empty_wiki_says_so(seeded):
    # TEAM_B has no compiled memory; TEAM_A always carries the seed's fact.
    section = wiki_section(TEAM_B)
    assert "empty" in section.lower()


def test_page_less_facts_still_surface(seeded):
    """The seed's fact predates pages, so it lives in the Uncategorized
    bucket — the agent must still be told the wiki has something in it."""
    section = wiki_section(TEAM_A)
    assert "Uncategorized" in section
    assert "empty" not in section.lower()


def test_index_is_team_scoped(seeded):
    conn = _admin()
    try:
        with conn.cursor() as cur:
            _seed_page(cur, TEAM_B, "OtherTeamSecrets", "not yours")
    finally:
        conn.close()
    assert "OtherTeamSecrets" not in wiki_section(TEAM_A)
