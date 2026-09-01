"""Ranked search over the wiki, beside the index — not instead of it.

findings §20.4, corrected 2026-08-18. The recall path is already index-and-
select: `agent/agent.py:wiki_section` puts every page title and description in
the system prompt on every turn, and `memory_read_page` pulls one page's body
on demand. That is Claude Code's shape and it is shipped.

The residual failure the benchmark points at is therefore narrower than
"the index was too small" — Comrade's index is complete. It is *"the agent
opened one page when the answer needed two."* Search covers that: index and
ranked search fail differently and cover each other, which is why Claude Code,
Warp (§20.7.2) and the article's own hybrid all ship both.

Lexical only — `tsvector` + `ts_rank_cd`. §20.4-3 makes pgvector a separate,
later decision taken only if lexical measurably misses. It does miss —
test_lexical_search_misses_a_near_synonym records the first instance — but one
stemmer quirk is a data point, not a rate, and the always-in-prompt page index
covers that case. Record the next one there before anyone reaches for vectors.
"""
import psycopg
import pytest

from agent.tools import search_memory
from shared.config import settings
from tests._seed import A1, B1, TEAM_A, TEAM_B, VER_A
from pipeline.parsers import SPACE_MARK

def unmarked(value):
    """Tool results are datamarked — spaces become SPACE_MARK — so a test that
    looks for ordinary prose has to undo the marking first.

    Added 2026-09-02 when spotlight() was extended from document_read to every
    read path. These assertions are about WHICH facts come back and what they
    say, not about the marking; the marking itself is the subject of
    tests/test_datamarking.py.
    """
    return value.replace(SPACE_MARK, " ") if isinstance(value, str) else value



@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def _fact(admin, team_id, text, page_title=None, active=True, archived=False):
    """One entry with one version, optionally on a titled page."""
    page_id = None
    if page_title:
        page_id = admin.execute(
            "insert into public.memory_pages (team_id, title) values (%s,%s)"
            " on conflict (team_id, title) do update set title=excluded.title"
            " returning id",
            (team_id, page_title),
        ).fetchone()[0]
    entry_id = admin.execute(
        "insert into public.memory_entries (team_id, page_id, archived)"
        " values (%s,%s,%s) returning id",
        (team_id, page_id, archived),
    ).fetchone()[0]
    return admin.execute(
        "insert into public.memory_versions (entry_id, team_id, fact,"
        " change_type, is_active) values (%s,%s,%s,'added',%s) returning id",
        (entry_id, team_id, text, active),
    ).fetchone()[0]


def test_it_finds_a_fact_by_its_words(seeded, admin):
    _fact(admin, TEAM_A, "the listing expires after 90 minutes", "Listings")
    hits = search_memory(TEAM_A, A1, "listing expires")
    assert any("90 minutes" in unmarked(h["fact"]) for h in hits)


def test_lexical_search_misses_a_near_synonym(seeded, admin):
    """Recorded as a MEASUREMENT, not fixed — and this is the measurement
    §20.4-3 asked for before anyone reaches for pgvector.

        select to_tsvector('english','the listing expires after 90 minutes'),
               plainto_tsquery('english','listing expiry');
        -> '90':5 'expir':3 'list':2 'minut':6  |  'list' & 'expiri'

    The Snowball stemmer takes "expires" to `expir` and "expiry" to `expiri`.
    Different stems, no match, and a member asking the obvious question in the
    obvious words gets nothing back.

    §20.4-3 made pgvector "a separate, later decision taken only if lexical
    alone measurably misses". It measurably misses. That is not a reason to add
    a vector index today — one contrived pair is not a rate — but it IS the
    first data point, and this test is where the next one gets recorded. The
    always-in-prompt page index still covers this case, which is the argument
    for shipping both.
    """
    _fact(admin, TEAM_A, "the listing expires after 90 minutes", "Listings")
    assert search_memory(TEAM_A, A1, "listing expiry") == []


def test_a_superseded_fact_never_comes_back(seeded, admin):
    """The one that would make search worse than no search.

    memory_versions is bi-temporal: a revised fact keeps its old row with
    is_active false. Search that ignores the flag would let the agent quote a
    value the team explicitly replaced, with a citation and a date, which reads
    as more authoritative than a guess rather than less.
    """
    _fact(admin, TEAM_A, "the listing expires after 2 hours", "Listings",
          active=False)
    _fact(admin, TEAM_A, "the listing expires after 90 minutes", "Listings")
    hits = search_memory(TEAM_A, A1, "listing expires")
    assert [unmarked(h["fact"]) for h in hits] == [
        "the listing expires after 90 minutes"
    ]


def test_an_archived_entry_is_gone_too(seeded, admin):
    """`archived` is the entry-level tombstone all_active_pages already
    respects; search must agree with the page view or the same fact is absent
    from the wiki and present in search."""
    _fact(admin, TEAM_A, "the archived thing about penguins", "Old",
          archived=True)
    assert search_memory(TEAM_A, A1, "penguins") == []


def test_it_does_not_cross_a_team_boundary(seeded, admin):
    """Read as the member, so au_memory_versions_select is the gate — but the
    explicit team_id matters independently: `authenticated` has no
    current_team(), so a member of two teams would otherwise search both at
    once and could not tell which wiki an answer came from."""
    _fact(admin, TEAM_B, "team B ships on tuesdays", "Releases")
    assert search_memory(TEAM_A, A1, "tuesdays") == []
    with_b = search_memory(TEAM_B, B1, "tuesdays")
    assert any("tuesdays" in unmarked(h["fact"]) for h in with_b)


def test_a_stranger_gets_nothing(seeded, admin):
    """Belt and braces: RLS should already refuse, and asking for a team you
    are not in must not fall back to unscoped results."""
    _fact(admin, TEAM_A, "something confidential about pricing", "Money")
    assert search_memory(TEAM_A, B1, "pricing") == []


def test_every_hit_carries_its_date_and_page(seeded, admin):
    """§20.3.1 measured a 39-point temporal gap and the fix was annotating
    facts at projection time. A search result is a projection: an undated hit
    reintroduces exactly the gap the wiki render just closed, and the page
    title is what lets the agent open the rest of the page.
    """
    _fact(admin, TEAM_A, "the demo is on 14 march", "Deadlines")
    hit = next(
        h for h in search_memory(TEAM_A, A1, "demo")
        if "demo" in unmarked(h["fact"])
    )
    assert hit["page"] == "Deadlines"
    assert hit["valid_from"] is not None


def test_a_citation_is_returned_when_there_is_one(seeded, admin):
    """Provenance, not decoration: §20.4's third advantage over the systems
    that scored 44.9% is that facts come back with where they came from."""
    # A real document: trg_memory_citation_source_team refuses a citation
    # whose source belongs to another team, and it is right to — a citation
    # pointing across the boundary would leak the excerpt with it.
    doc_id = admin.execute(
        "insert into public.documents (team_id, kind, filename)"
        " values (%s,'text','brief.txt') returning id",
        (TEAM_A,),
    ).fetchone()[0]
    admin.execute(
        "insert into public.memory_citations (version_id, source_kind, source_id,"
        " excerpt) values (%s,'document',%s,'the brief said friday')",
        (VER_A, doc_id),
    )
    hit = next(
        h for h in search_memory(TEAM_A, A1, "deadline friday")
        if unmarked(h["fact"]) == "deadline is Friday"
    )
    assert hit["source_kind"] == "document"


def test_no_match_is_an_empty_list_not_an_error(seeded):
    """The agent has to be able to say "the wiki does not record that". An
    exception here would surface as a failed turn instead."""
    assert search_memory(TEAM_A, A1, "xylophone marsupial") == []


def test_an_empty_query_returns_nothing_rather_than_everything(seeded, admin):
    """plainto_tsquery('') matches nothing, but a caller trimming to empty
    must not accidentally become "dump the wiki" if that ever changes."""
    _fact(admin, TEAM_A, "a fact that exists", "Things")
    assert search_memory(TEAM_A, A1, "   ") == []


def test_results_are_capped(seeded, admin):
    """Search feeds a prompt. Uncapped, one broad query undoes the
    consolidation cap from the other direction."""
    for i in range(12):
        _fact(admin, TEAM_A, f"deployment note number {i}", "Deploys")
    assert len(search_memory(TEAM_A, A1, "deployment note", limit=5)) == 5


def test_the_best_match_comes_first(seeded, admin):
    """ts_rank_cd, not insertion order — an agent reading the top hit should
    be reading the most relevant one."""
    _fact(admin, TEAM_A, "unrelated note mentioning deployment once", "Notes")
    _fact(admin, TEAM_A, "deployment deployment checklist for deployment day",
          "Deploys")
    hits = search_memory(TEAM_A, A1, "deployment")
    assert "checklist" in unmarked(hits[0]["fact"])


def test_the_tool_is_declared_a_read(seeded):
    """agent/registry.py fails closed — an unregistered tool resolves to
    outbound/writes/needs_human, which would put a consent card in front of a
    plain wiki search."""
    from agent.registry import spec_for

    spec = spec_for("memory_search")
    assert spec.surface == "db"
    assert spec.writes is False
    assert spec.needs_human is False


def test_a_member_of_two_teams_searches_only_the_one_they_asked_about(seeded, admin):
    """The case the explicit team_id exists for, and the only one that proves
    it does anything.

    Mutation-checked: removing `v.team_id = %(team_id)s` left every other test
    in this file green, because they all use a member of ONE team and RLS
    already refuses the other. `authenticated` has no current_team(), so for
    somebody in two teams RLS permits both wikis at once — the result set would
    silently blend them, and the agent would answer a question about this team
    with the other team's facts, dated and cited and completely wrong.
    """
    admin.execute(
        "insert into public.memberships (team_id, user_id, role, status, joined_at)"
        " values (%s,%s,'member','active', now())",
        (TEAM_B, A1),
    )
    _fact(admin, TEAM_A, "team A deploys on fridays", "Releases")
    _fact(admin, TEAM_B, "team B deploys on tuesdays", "Releases")

    a_hits = [unmarked(h["fact"]) for h in search_memory(TEAM_A, A1, "deploys")]
    assert a_hits == ["team A deploys on fridays"]

    b_hits = [unmarked(h["fact"]) for h in search_memory(TEAM_B, A1, "deploys")]
    assert b_hits == ["team B deploys on tuesdays"]
