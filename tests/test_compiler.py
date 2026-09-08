"""Compiler v2: pure prompt/validation units + deterministic apply step (no LLM)."""
from types import SimpleNamespace

import psycopg
import pytest

from pipeline.compiler import (
    DEFAULT_PAGE_TITLE, REJECT, Candidate, Decision, apply_compilation,
    build_consolidation_prompt, validate_decisions,
)
from shared.config import settings
from shared.db import Role, team_session
from tests._seed import TEAM_A

DOC = "d0000000-0000-0000-0000-0000000000d1"


def _page(title, facts, description=""):
    return {"page_id": "p-x", "title": title, "description": description,
            "facts": facts}


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _source_document() -> str:
    """Create the provenance source required by the citation trigger."""
    conn = _admin()
    try:
        conn.execute(
            "insert into public.documents (id, team_id, kind, filename)"
            " values (%s,%s,'text','source.txt')",
            (DOC, TEAM_A),
        )
    finally:
        conn.close()
    return DOC


def _seed_entry(fact="Deadline is Thursday"):
    conn = _admin()
    try:
        entry_id = conn.execute(
            "insert into public.memory_entries (team_id) values (%s) returning id",
            (TEAM_A,),
        ).fetchone()[0]
        conn.execute(
            "insert into public.memory_versions (entry_id, team_id, fact,"
            " change_type) values (%s,%s,%s,'added')",
            (entry_id, TEAM_A, fact),
        )
        return str(entry_id)
    finally:
        conn.close()


# ---------- pure units ----------

def test_prompt_shows_wiki_by_page_then_candidates():
    cands = [Candidate(text="Deadline is Friday", excerpt="due Fri")]
    pages = [_page("Deadlines", [{"entry_id": "e-1", "text": "Deadline is Thursday"}],
                   description="key dates")]
    prompt = build_consolidation_prompt(cands, pages)
    assert "## Page: Deadlines — key dates" in prompt
    assert "[e-1] Deadline is Thursday" in prompt
    assert "### Candidate 0" in prompt
    assert "Deadline is Friday" in prompt
    # the wiki appears once, before the candidates
    assert prompt.index("## Page: Deadlines") < prompt.index("### Candidate 0")


def test_prompt_marks_empty_page_and_empty_wiki():
    prompt = build_consolidation_prompt(
        [Candidate(text="New fact")], [_page("Empty", [])],
    )
    assert "(no facts yet)" in prompt
    prompt = build_consolidation_prompt([Candidate(text="New fact")], [])
    assert "(wiki is empty)" in prompt


def test_validate_keeps_one_decision_per_candidate_in_order():
    """🔴 The two malformed cases here used to become `add` (fix.md F12), so
    a hallucinated entry id and a candidate the model never mentioned were
    both PUBLISHED. They are rejected now; what this test still pins is the
    shape — one decision per candidate, in order, duplicates and unknown
    indices ignored.
    """
    cands = [Candidate(text="a"), Candidate(text="b"), Candidate(text="c")]
    pages = [_page("P", [{"entry_id": "e-1", "text": "x"}])]
    decisions = [
        Decision(candidate_index=0, action="revise", entry_id="e-1"),   # valid
        Decision(candidate_index=1, action="revise", entry_id="e-9"),   # unknown target
        # candidate 2 missing
        Decision(candidate_index=0, action="noop", entry_id="e-1"),     # duplicate -> ignored
        Decision(candidate_index=9, action="add"),                      # unknown index -> ignored
    ]
    out = validate_decisions(cands, pages, decisions)
    assert [(d.candidate_index, d.action) for d in out] == [
        (0, "revise"), (1, REJECT), (2, REJECT),
    ]


def test_validate_rejects_unknown_action():
    """🔴 This test was always named for what it should do, and asserted
    `== "add"`. The name described the intent; the assertion encoded the
    defect, and the two sat next to each other unremarked."""
    out = validate_decisions(
        [Candidate(text="a")], [],
        [Decision(candidate_index=0, action="obliterate")],
    )
    assert out[0].action == REJECT


def test_validate_normalises_page_title():
    out = validate_decisions(
        [Candidate(text="a"), Candidate(text="b")], [],
        [
            Decision(candidate_index=0, action="add", page_title="  Deadlines "),
            Decision(candidate_index=1, action="add", page_title="   "),
        ],
    )
    assert out[0].page_title == "Deadlines"
    assert out[1].page_title is None


def test_apply_rejects_misaligned_inputs():
    with pytest.raises(ValueError, match="equal lengths"):
        apply_compilation(
            None, TEAM_A, [Candidate(text="fact")], [], [None],
        )


# ---------- apply step (DB) ----------

def test_apply_add_writes_fact_provenance_citation_card(seeded):
    cands = [Candidate(text="Deadline is Friday", excerpt="due Friday")]
    decs = [Decision(candidate_index=0, action="add")]
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        result = apply_compilation(conn, TEAM_A, cands, decs, [("document", _source_document())] * len(cands))
    assert (result["added"], result["revised"], result["removed"]) == (1, 0, 0)
    conn = _admin()
    try:
        row = conn.execute(
            "select fact from public.memory_versions"
            " where compilation_id=%s", (result["compilation_id"],),
        ).fetchone()
        assert row == ("Deadline is Friday",)
        card = conn.execute(
            "select body from public.messages where id=%s",
            (result["diff_message_id"],),
        ).fetchone()[0]
        assert card == "Memory updated — 1 added, 0 revised, 0 removed."
        assert conn.execute(
            "select t.title from public.messages m join public.threads t"
            " on t.id=m.thread_id and t.team_id=m.team_id where m.id=%s",
            (result["diff_message_id"],),
        ).fetchone()[0] == "General"
    finally:
        conn.close()


def _active_version(entry_id: str) -> str:
    """The version a real consolidation would have been shown.

    `consolidate` binds this for every revision now (fix.md F14): a null
    expectation used to match whatever was active, which let a compile
    supersede a version it had never read.
    """
    conn = _admin()
    try:
        return str(conn.execute(
            "select id from public.memory_versions"
            " where entry_id=%s and is_active", (entry_id,),
        ).fetchone()[0])
    finally:
        conn.close()


def test_apply_revise_supersedes(seeded):
    entry_id = _seed_entry()
    cands = [Candidate(text="Deadline is Friday", excerpt="moved")]
    decs = [Decision(candidate_index=0, action="revise", entry_id=entry_id,
                     seen_version_id=_active_version(entry_id))]
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        result = apply_compilation(conn, TEAM_A, cands, decs, [("document", _source_document())] * len(cands))
    assert result["revised"] == 1
    conn = _admin()
    try:
        rows = conn.execute(
            "select fact, is_active, valid_until is not null"
            " from public.memory_versions where entry_id=%s order by created_at",
            (entry_id,),
        ).fetchall()
        assert ("Deadline is Thursday", False, True) in rows
        assert ("Deadline is Friday", True, False) in rows
    finally:
        conn.close()


def test_apply_invalidate_tombstones_without_replacement(seeded):
    entry_id = _seed_entry("Mobile app is planned")
    cands = [Candidate(text="Mobile app was dropped", excerpt="drop the mobile app")]
    decs = [Decision(candidate_index=0, action="invalidate", entry_id=entry_id,
                     seen_version_id=_active_version(entry_id))]
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        result = apply_compilation(conn, TEAM_A, cands, decs, [("document", _source_document())] * len(cands))
    assert (result["added"], result["removed"]) == (0, 1)
    conn = _admin()
    try:
        rows = conn.execute(
            "select change_type, is_active from public.memory_versions"
            " where entry_id=%s order by created_at", (entry_id,),
        ).fetchall()
        assert rows == [("added", False), ("invalidated", False)]  # no active fact left
        card = conn.execute(
            "select body from public.messages where id=%s",
            (result["diff_message_id"],),
        ).fetchone()[0]
        assert card == "Memory updated — 0 added, 0 revised, 1 removed."
        removed = conn.execute(
            "select entries_removed from public.memory_compilations where id=%s",
            (result["compilation_id"],),
        ).fetchone()[0]
        assert removed == 1
    finally:
        conn.close()


def test_apply_noop_writes_nothing(seeded):
    entry_id = _seed_entry("Deadline is Friday")
    cands = [Candidate(text="Deadline is Friday")]
    decs = [Decision(candidate_index=0, action="noop", entry_id=entry_id)]
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        result = apply_compilation(conn, TEAM_A, cands, decs, [("document", _source_document())] * len(cands))
    assert result["skipped"] == 1 and result["added"] == 0
    conn = _admin()
    try:
        n = conn.execute(
            "select count(*) from public.memory_versions where compilation_id=%s",
            (result["compilation_id"],),
        ).fetchone()[0]
        assert n == 0
    finally:
        conn.close()


def test_apply_add_routes_to_named_page_and_reuses_case_insensitively(seeded):
    cands = [Candidate(text="Demo is Friday"), Candidate(text="Report due Monday")]
    decs = [
        Decision(candidate_index=0, action="add", page_title="Deadlines"),
        Decision(candidate_index=1, action="add", page_title="deadlines"),
    ]
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        apply_compilation(conn, TEAM_A, cands, decs, [("document", _source_document())] * len(cands))
    conn = _admin()
    try:
        pages = conn.execute(
            "select id, title from public.memory_pages where team_id=%s",
            (TEAM_A,),
        ).fetchall()
        assert len(pages) == 1 and pages[0][1] == "Deadlines"  # reused, not duplicated
        n = conn.execute(
            "select count(*) from public.memory_entries"
            " where team_id=%s and page_id=%s",
            (TEAM_A, pages[0][0]),
        ).fetchone()[0]
        assert n == 2
    finally:
        conn.close()


def test_apply_add_without_page_title_lands_on_default_page(seeded):
    cands = [Candidate(text="Orphanish fact")]
    decs = [Decision(candidate_index=0, action="add")]
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        apply_compilation(conn, TEAM_A, cands, decs, [("document", _source_document())] * len(cands))
    conn = _admin()
    try:
        title = conn.execute(
            "select p.title from public.memory_entries e"
            " join public.memory_pages p on p.id = e.page_id"
            " where e.team_id=%s order by e.created_at desc limit 1",
            (TEAM_A,),
        ).fetchone()[0]
        assert title == DEFAULT_PAGE_TITLE
    finally:
        conn.close()


def test_apply_rejects_a_decision_naming_an_unknown_entry(seeded):
    """🔴 This test used to assert `added == 1`: a decision naming an entry
    that does not exist FELL BACK to add, so a hallucinated entry id did not
    fail and did not get rejected — it got published as a brand new fact.

    T18 rejects it instead. A model that invented an id has told us nothing
    about where this candidate belongs, and inventing a home for it is the
    system agreeing."""
    cands = [Candidate(text="Orphan fact")]
    decs = [Decision(candidate_index=0, action="revise",
                     entry_id="00000000-0000-0000-0000-0000000000ff")]
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        result = apply_compilation(conn, TEAM_A, cands, decs, [("document", _source_document())] * len(cands))
    assert result["rejected"] == 1
    assert result["added"] == 0 and result["revised"] == 0


def test_a_new_page_gets_its_description_written(seeded):
    """findings §2.3 + §20.4-1: descriptions feed the LIVE recall index."""
    candidates = [Candidate(text="We ship on Fridays", excerpt="ship Fridays")]
    decisions = [
        Decision(candidate_index=0, action="add", page_title="Release Cadence",
                 page_description="how and when we ship")
    ]
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        apply_compilation(conn, TEAM_A, candidates, decisions, [None])
        row = conn.execute(
            "select description from public.memory_pages"
            " where team_id=%s and title='Release Cadence'",
            (TEAM_A,),
        ).fetchone()
    assert row[0] == "how and when we ship"


def test_validate_normalises_a_blank_description():
    candidates = [Candidate(text="x")]
    raw = [Decision(candidate_index=0, action="add", page_title="P",
                    page_description="   ")]
    out = validate_decisions(candidates, [], raw)
    assert out[0].page_description is None
