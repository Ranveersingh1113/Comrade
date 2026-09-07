"""What building the agent's prompt costs as a team's wiki grows.

🔴 THE DEFECT. `wiki_section` renders TITLES AND DESCRIPTIONS ONLY — and built
them by calling `all_active_pages`, which loads every active fact of every
page with its `valid_from` and its first citation kind. That happens on EVERY
turn. A team with two thousand facts paid for two thousand rows to print a
list of page names, and the cost grew with the wiki forever.

`read_memory_page` did the same to open ONE page: load every page's every
fact, then pick one out of the list in Python.
"""
import psycopg
import pytest

from shared.config import settings
from tests._seed import A1, TEAM_A


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _wiki(pages: int, facts_per_page: int) -> None:
    """A wiki of a given size, written straight in."""
    conn = _admin()
    try:
        conn.execute("delete from public.memory_pages where team_id=%s", (TEAM_A,))
        conn.execute("delete from public.memory_entries where team_id=%s", (TEAM_A,))
        comp = conn.execute(
            "insert into public.memory_compilations (team_id, trigger, status)"
            " values (%s,'scheduled','done') returning id",
            (TEAM_A,),
        ).fetchone()[0]
        for p in range(pages):
            page_id = conn.execute(
                "insert into public.memory_pages (team_id, title, description, kind)"
                " values (%s,%s,%s,'fact') returning id",
                (TEAM_A, f"Page {p}", f"what page {p} is for"),
            ).fetchone()[0]
            for f in range(facts_per_page):
                entry_id = conn.execute(
                    "insert into public.memory_entries (team_id, page_id)"
                    " values (%s,%s) returning id",
                    (TEAM_A, page_id),
                ).fetchone()[0]
                conn.execute(
                    "insert into public.memory_versions (entry_id, team_id,"
                    " compilation_id, fact, change_type)"
                    " values (%s,%s,%s,%s,'added')",
                    (entry_id, TEAM_A, comp, f"page {p} fact {f}"),
                )
    finally:
        conn.close()


class _Counting:
    """A connection that records the SQL run through it."""

    def __init__(self, inner):
        self._inner = inner
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        return self._inner.execute(sql, params) if params else self._inner.execute(sql)


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------

def test_the_page_index_does_not_read_a_single_fact(seeded):
    """🔴 It read every fact of every page to print a list of names."""
    from pipeline.wiki import page_index
    from shared.db import user_session

    _wiki(pages=5, facts_per_page=20)

    with user_session(A1) as raw:
        conn = _Counting(raw)
        pages = page_index(conn, TEAM_A)

    assert [p["title"] for p in pages] == [f"Page {i}" for i in range(5)]
    assert all("facts" not in p for p in pages)
    sql = " ".join(conn.statements).lower()
    # An EXISTS against memory_versions is right — "does this page have
    # anything on it" is the question. Fetching the fact text to answer it is
    # the bug, so that is what this forbids.
    assert "v.fact" not in sql, ("the index fetched fact text: " + sql)
    # A fixed number of statements, not one per page: two, the index and the
    # check for facts that predate pages.
    assert len(conn.statements) <= 2, (
        f"{len(conn.statements)} statements to build an index of 5 pages"
    )


def test_the_index_omits_pages_with_nothing_on_them(seeded):
    """An index that lists an empty page sends the agent to open nothing."""
    from pipeline.wiki import page_index
    from shared.db import user_session

    _wiki(pages=2, facts_per_page=3)
    conn = _admin()
    try:
        conn.execute(
            "insert into public.memory_pages (team_id, title, description, kind)"
            " values (%s,'Empty','nothing here','fact')",
            (TEAM_A,),
        )
    finally:
        conn.close()

    with user_session(A1) as conn:
        titles = [p["title"] for p in page_index(conn, TEAM_A)]

    assert "Empty" not in titles


def test_the_index_is_bounded(seeded):
    """A wiki with three hundred pages must not put three hundred lines in
    every prompt: the index is paid on every turn, of every thread."""
    from pipeline.wiki import MAX_INDEX_PAGES, page_index
    from shared.db import user_session

    _wiki(pages=MAX_INDEX_PAGES + 10, facts_per_page=1)

    with user_session(A1) as conn:
        pages = page_index(conn, TEAM_A)

    assert len(pages) == MAX_INDEX_PAGES


def test_the_rendered_index_says_when_it_was_truncated(seeded):
    """Silently showing part of the wiki teaches the agent the rest does not
    exist, and it will say so to the team with confidence."""
    from agent.agent import wiki_section
    from pipeline.wiki import MAX_INDEX_PAGES

    _wiki(pages=MAX_INDEX_PAGES + 5, facts_per_page=1)

    text = wiki_section(TEAM_A, A1)

    assert "memory_search" in text or "more page" in text.lower()


# ---------------------------------------------------------------------------
# Reading one page
# ---------------------------------------------------------------------------

def test_opening_one_page_reads_only_that_page(seeded):
    """🔴 It loaded every page's every fact and picked one out in Python."""
    from agent.tools import read_memory_page

    _wiki(pages=6, facts_per_page=10)

    page = read_memory_page(TEAM_A, A1, "Page 3")

    # The tool's shape is {fact, citations} — spotlighted, so compare on the
    # words rather than the spacing.
    assert len(page["facts"]) == 10
    assert all("page" in f["fact"] and "3" in f["fact"] for f in page["facts"])


def test_an_unknown_title_still_offers_the_available_ones(seeded):
    from agent.tools import read_memory_page

    _wiki(pages=3, facts_per_page=2)

    page = read_memory_page(TEAM_A, A1, "Nonexistent")

    assert page["error"] == "no such page"
    assert "Page 1" in page["available"]


def test_a_title_is_matched_without_case(seeded):
    from agent.tools import read_memory_page

    _wiki(pages=2, facts_per_page=1)

    assert "facts" in read_memory_page(TEAM_A, A1, "page 1")


def test_one_enormous_page_cannot_blow_the_budget(seeded):
    """🔴 A single page with a thousand facts went into the prompt whole. The
    consolidation cap counted CANDIDATES, and nothing capped what one page
    could contribute to a turn."""
    from agent.tools import MAX_PAGE_FACTS, read_memory_page

    _wiki(pages=1, facts_per_page=MAX_PAGE_FACTS + 40)

    page = read_memory_page(TEAM_A, A1, "Page 0")

    assert len(page["facts"]) == MAX_PAGE_FACTS
    assert page.get("truncated") is True, (
        "a page cut short must say so, or the agent reports absence as fact"
    )
