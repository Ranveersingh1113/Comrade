"""Compiler apply step (deterministic): adds, revisions/supersession, citations,
diff card, run counts. No LLM — fact ops + vectors are hand-built."""
from types import SimpleNamespace

import psycopg

from pipeline.compiler import apply_compilation
from shared.config import settings
from shared.db import Role, team_session
from tests._seed import TEAM_A

DOC = "d0000000-0000-0000-0000-0000000000d1"  # source_id has no FK
VEC = [0.1] * 1536


def _op(text, change_type="added", revises_entry_id=None, excerpt=""):
    return SimpleNamespace(
        text=text, change_type=change_type,
        revises_entry_id=revises_entry_id, excerpt=excerpt,
    )


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def test_apply_adds_facts_with_citations_and_card(seeded):
    ops = [
        _op("Deadline is Friday", excerpt="due Friday"),
        _op("Alice owns the backend", excerpt="Alice: backend"),
    ]
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        result = apply_compilation(conn, TEAM_A, DOC, ops, [VEC, VEC])

    assert result["added"] == 2 and result["revised"] == 0
    conn = _admin()
    try:
        # scope to THIS compilation (seed already inserts one memory fact)
        produced = conn.execute(
            "select count(*) from public.memory_versions"
            " where compilation_id=%s and is_active",
            (result["compilation_id"],),
        ).fetchone()[0]
        assert produced == 2
        cites = conn.execute(
            "select count(*) from public.memory_citations c"
            " join public.memory_versions v on v.id=c.version_id"
            " where v.compilation_id=%s",
            (result["compilation_id"],),
        ).fetchone()[0]
        assert cites == 2
        card = conn.execute(
            "select body, thread_type, sender_kind from public.messages where id=%s",
            (result["diff_message_id"],),
        ).fetchone()
        assert card == ("Memory updated — 2 added, 0 revised.", "group", "ai")
        comp = conn.execute(
            "select status, entries_added, entries_revised"
            " from public.memory_compilations where id=%s",
            (result["compilation_id"],),
        ).fetchone()
        assert comp == ("done", 2, 0)
    finally:
        conn.close()


def test_apply_revise_supersedes_prior_version(seeded):
    conn = _admin()
    try:
        entry_id = conn.execute(
            "insert into public.memory_entries (team_id) values (%s) returning id",
            (TEAM_A,),
        ).fetchone()[0]
        conn.execute(
            "insert into public.memory_versions (entry_id, team_id, fact, change_type)"
            " values (%s,%s,'Deadline is Thursday','added')",
            (entry_id, TEAM_A),
        )
    finally:
        conn.close()

    ops = [_op("Deadline is Friday", "revised", str(entry_id), "moved to Friday")]
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        result = apply_compilation(conn, TEAM_A, DOC, ops, [VEC])

    assert result["revised"] == 1 and result["added"] == 0
    conn = _admin()
    try:
        rows = conn.execute(
            "select fact, is_active from public.memory_versions"
            " where entry_id=%s order by created_at",
            (entry_id,),
        ).fetchall()
        active = [r for r in rows if r[1]]
        assert len(active) == 1 and active[0][0] == "Deadline is Friday"
        assert any(r[0] == "Deadline is Thursday" and not r[1] for r in rows)
    finally:
        conn.close()


def test_apply_invalid_revise_falls_back_to_added(seeded):
    ops = [_op("Orphan fact", "revised", "00000000-0000-0000-0000-0000000000ff")]
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        result = apply_compilation(conn, TEAM_A, DOC, ops, [VEC])
    assert result["added"] == 1 and result["revised"] == 0
