"""Wiki projection: page grouping, orphan bucket, index, rendering (no LLM)."""
import psycopg

from pipeline.wiki import ORPHAN_TITLE, all_active_pages, render_team_wiki
from shared.config import settings
from shared.db import Role, team_session
from tests._seed import TEAM_A, TEAM_B


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _seed_page_with_fact(cur, team_id, title, fact, description=""):
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
    cur.execute(
        "insert into public.memory_versions (entry_id, team_id, fact, change_type)"
        " values (%s,%s,%s,'added')",
        (entry_id, team_id, fact),
    )
    return str(page_id)


def test_pages_group_facts_and_orphans_bucket(seeded):
    conn = _admin()
    try:
        with conn.cursor() as cur:
            _seed_page_with_fact(cur, TEAM_A, "Deadlines", "Demo is Friday",
                                 description="key dates")
    finally:
        conn.close()
    with team_session(Role.PIPELINE, TEAM_A) as s:
        pages = all_active_pages(s, TEAM_A)
    titles = {p["title"] for p in pages}
    assert "Deadlines" in titles
    # seed() creates one page-less fact ("deadline is Friday") -> orphan bucket
    assert ORPHAN_TITLE in titles
    deadlines = next(p for p in pages if p["title"] == "Deadlines")
    assert deadlines["description"] == "key dates"
    assert [f["text"] for f in deadlines["facts"]] == ["Demo is Friday"]


def test_pages_scoped_to_team(seeded):
    conn = _admin()
    try:
        with conn.cursor() as cur:
            _seed_page_with_fact(cur, TEAM_B, "B-Page", "B secret")
    finally:
        conn.close()
    with team_session(Role.PIPELINE, TEAM_A) as s:
        titles = {p["title"] for p in all_active_pages(s, TEAM_A)}
    assert "B-Page" not in titles


def test_render_wiki_markdown(seeded):
    conn = _admin()
    try:
        with conn.cursor() as cur:
            _seed_page_with_fact(cur, TEAM_A, "Deadlines", "Demo is Friday",
                                 description="key dates")
            # empty page must be skipped in the rendering
            cur.execute(
                "insert into public.memory_pages (team_id, title) values (%s,'Empty')",
                (TEAM_A,),
            )
    finally:
        conn.close()
    with team_session(Role.PIPELINE, TEAM_A) as s:
        md = render_team_wiki(s, TEAM_A)
    assert md.startswith("# Team wiki")
    assert "## Deadlines" in md
    assert "_key dates_" in md
    assert "- Demo is Friday" in md
    assert "## Empty" not in md
    assert f"## {ORPHAN_TITLE}" in md  # seed's page-less fact still visible
