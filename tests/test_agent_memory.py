"""The agent's view of the team wiki (Claude Code's auto-loaded memory)."""
import psycopg

from agent.agent import wiki_section
from shared.config import settings
from tests._seed import A1, B1, TEAM_A, TEAM_B


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

    section = wiki_section(TEAM_A, A1)
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
    assert "Hollow" not in wiki_section(TEAM_A, A1)


def test_empty_wiki_says_so(seeded):
    # TEAM_B has no compiled memory; TEAM_A always carries the seed's fact.
    section = wiki_section(TEAM_B, B1)
    assert "empty" in section.lower()


def test_page_less_facts_still_surface(seeded):
    """The seed's fact predates pages, so it lives in the Uncategorized
    bucket — the agent must still be told the wiki has something in it."""
    section = wiki_section(TEAM_A, A1)
    assert "Uncategorized" in section
    assert "empty" not in section.lower()


def test_index_is_team_scoped(seeded):
    conn = _admin()
    try:
        with conn.cursor() as cur:
            _seed_page(cur, TEAM_B, "OtherTeamSecrets", "not yours")
    finally:
        conn.close()
    assert "OtherTeamSecrets" not in wiki_section(TEAM_A, A1)


def test_read_page_returns_facts_with_citations(seeded):
    from agent.tools import read_memory_page

    conn = _admin()
    try:
        with conn.cursor() as cur:
            _, _, version_id = _seed_page(
                cur, TEAM_A, "Deadlines", "Demo is Friday", description="key dates",
            )
            doc_id = cur.execute(
                "insert into public.documents (team_id, kind, filename)"
                " values (%s,'text','plan.txt') returning id",
                (TEAM_A,),
            ).fetchone()[0]
            cur.execute(
                "insert into public.memory_citations (version_id, source_kind,"
                " source_id, excerpt) values (%s,'document',%s,'demo on Friday')",
                (version_id, doc_id),
            )
    finally:
        conn.close()

    page = read_memory_page(TEAM_A, A1, "Deadlines")
    assert page["title"] == "Deadlines"
    assert page["facts"][0]["fact"] == "Demo is Friday"
    citation = page["facts"][0]["citations"][0]
    assert citation["source_kind"] == "document"
    assert citation["excerpt"] == "demo on Friday"


def test_read_page_is_case_insensitive(seeded):
    from agent.tools import read_memory_page

    conn = _admin()
    try:
        with conn.cursor() as cur:
            _seed_page(cur, TEAM_A, "Deadlines", "Demo is Friday")
    finally:
        conn.close()
    assert read_memory_page(TEAM_A, A1, "deadlines")["title"] == "Deadlines"


def test_unknown_page_lists_what_is_available(seeded):
    from agent.tools import read_memory_page

    conn = _admin()
    try:
        with conn.cursor() as cur:
            _seed_page(cur, TEAM_A, "Deadlines", "Demo is Friday")
    finally:
        conn.close()

    out = read_memory_page(TEAM_A, A1, "Budget")
    assert out["error"] == "no such page"
    assert "Deadlines" in out["available"]


def test_read_page_cannot_reach_another_team(seeded):
    from agent.tools import read_memory_page

    conn = _admin()
    try:
        with conn.cursor() as cur:
            _seed_page(cur, TEAM_B, "OtherTeamSecrets", "not yours")
    finally:
        conn.close()

    out = read_memory_page(TEAM_A, A1, "OtherTeamSecrets")
    assert out["error"] == "no such page"
