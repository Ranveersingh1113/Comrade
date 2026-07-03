"""Compiler v2: pure prompt/validation units + deterministic apply step (no LLM)."""
from types import SimpleNamespace

import psycopg

from pipeline.compiler import (
    Candidate, Decision, apply_compilation, build_consolidation_prompt,
    validate_decisions,
)
from shared.config import settings
from shared.db import Role, team_session
from shared.embeddings import DIM, MODEL
from tests._seed import TEAM_A

DOC = "d0000000-0000-0000-0000-0000000000d1"  # source_id has no FK
VEC = [0.1] * 1536


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


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

def test_prompt_lists_candidates_with_their_neighbors():
    cands = [Candidate(text="Deadline is Friday", excerpt="due Fri")]
    neigh = [[{"entry_id": "e-1", "text": "Deadline is Thursday"}]]
    prompt = build_consolidation_prompt(cands, neigh)
    assert "### Candidate 0" in prompt
    assert "Deadline is Friday" in prompt
    assert "[e-1] Deadline is Thursday" in prompt


def test_prompt_marks_empty_neighbor_sets():
    prompt = build_consolidation_prompt([Candidate(text="New fact")], [[]])
    assert "(none)" in prompt


def test_validate_fills_missing_and_fixes_bad_targets():
    cands = [Candidate(text="a"), Candidate(text="b"), Candidate(text="c")]
    neigh = [[{"entry_id": "e-1", "text": "x"}], [], []]
    decisions = [
        Decision(candidate_index=0, action="revise", entry_id="e-1"),   # valid
        Decision(candidate_index=1, action="revise", entry_id="e-9"),   # bad target -> add
        # candidate 2 missing -> add
        Decision(candidate_index=0, action="noop", entry_id="e-1"),     # duplicate -> ignored
        Decision(candidate_index=9, action="add"),                      # unknown index -> ignored
    ]
    out = validate_decisions(cands, neigh, decisions)
    assert [(d.candidate_index, d.action) for d in out] == [
        (0, "revise"), (1, "add"), (2, "add"),
    ]


def test_validate_rejects_unknown_action():
    out = validate_decisions(
        [Candidate(text="a")], [[]],
        [Decision(candidate_index=0, action="obliterate")],
    )
    assert out[0].action == "add"


# ---------- apply step (DB) ----------

def test_apply_add_writes_fact_provenance_citation_card(seeded):
    cands = [Candidate(text="Deadline is Friday", excerpt="due Friday")]
    decs = [Decision(candidate_index=0, action="add")]
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        result = apply_compilation(conn, TEAM_A, DOC, cands, decs, [VEC])
    assert (result["added"], result["revised"], result["removed"]) == (1, 0, 0)
    conn = _admin()
    try:
        row = conn.execute(
            "select fact, embedding_model, embedding_dim from public.memory_versions"
            " where compilation_id=%s", (result["compilation_id"],),
        ).fetchone()
        assert row == ("Deadline is Friday", MODEL, DIM)
        card = conn.execute(
            "select body from public.messages where id=%s",
            (result["diff_message_id"],),
        ).fetchone()[0]
        assert card == "Memory updated — 1 added, 0 revised, 0 removed."
    finally:
        conn.close()


def test_apply_revise_supersedes(seeded):
    entry_id = _seed_entry()
    cands = [Candidate(text="Deadline is Friday", excerpt="moved")]
    decs = [Decision(candidate_index=0, action="revise", entry_id=entry_id)]
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        result = apply_compilation(conn, TEAM_A, DOC, cands, decs, [VEC])
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
    decs = [Decision(candidate_index=0, action="invalidate", entry_id=entry_id)]
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        result = apply_compilation(conn, TEAM_A, DOC, cands, decs, [VEC])
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
        result = apply_compilation(conn, TEAM_A, DOC, cands, decs, [VEC])
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


def test_apply_bad_target_falls_back_to_add(seeded):
    cands = [Candidate(text="Orphan fact")]
    decs = [Decision(candidate_index=0, action="revise",
                     entry_id="00000000-0000-0000-0000-0000000000ff")]
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        result = apply_compilation(conn, TEAM_A, DOC, cands, decs, [VEC])
    assert result["added"] == 1 and result["revised"] == 0
