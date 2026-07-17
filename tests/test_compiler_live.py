"""Live two-stage compile: real Gemini extract + consolidate + embeddings.

Skipped without a Gemini key. Model nondeterminism note: the revise assertion
tolerates 'revised' OR 'invalidated' on the target entry (both supersede);
what it must NOT be is an untouched old fact alongside a contradicting new one.
"""
import psycopg
import pytest

from pipeline.chat import compile_messages
from pipeline.compiler import compile_document
from pipeline.parsers import spotlight
from shared.config import settings
from tests._seed import A2, TEAM_A

pytestmark = pytest.mark.skipif(
    not settings.gemini_api_key, reason="no GEMINI_API_KEY configured"
)

DOC1 = "d0000000-0000-0000-0000-0000000000d1"
DOC2 = "d0000000-0000-0000-0000-0000000000d2"


def test_two_stage_compile_and_revise_roundtrip(seeded):
    r1 = compile_document(
        TEAM_A, DOC1,
        spotlight("Project plan: the final demo deadline is Friday, December 18."
                  " Alice owns the backend API."),
    )
    assert r1["added"] >= 1

    r2 = compile_document(
        TEAM_A, DOC2,
        spotlight("Update: the final demo deadline has moved to Monday, December 21."),
    )
    # The deadline fact must have been superseded, not duplicated.
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        active_deadlines = conn.execute(
            "select v.fact from public.memory_versions v"
            " join public.memory_entries e on e.id=v.entry_id"
            " where e.team_id=%s and v.is_active and v.fact ilike '%%december%%'",
            (TEAM_A,),
        ).fetchall()
    finally:
        conn.close()
    assert len(active_deadlines) <= 1, f"contradictory facts coexist: {active_deadlines}"
    assert r2["revised"] + r2["removed"] + r2["added"] >= 1


def test_chat_correction_supersedes_and_cites_message(seeded):
    compile_document(
        TEAM_A, DOC1,
        spotlight("Project plan: the final demo deadline is Friday, December 18."),
    )
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        rows = conn.execute(
            "insert into public.messages (team_id, thread_type, sender_kind,"
            " sender_id, body) values"
            " (%(t)s,'group','user',%(u)s,'heads up — the final demo deadline"
            " moved to Monday, December 21'),"
            " (%(t)s,'group','user',%(u)s,'thanks, noted!')"
            " returning id, created_at",
            {"t": TEAM_A, "u": A2},
        ).fetchall()
    finally:
        conn.close()
    messages = [
        {"id": str(r[0]), "sender": "A2", "text": b, "created_at": r[1]}
        for r, b in zip(rows, [
            "heads up — the final demo deadline moved to Monday, December 21",
            "thanks, noted!",
        ])
    ]
    through = max(m["created_at"] for m in messages)

    result = compile_messages(TEAM_A, messages, through)
    assert result["revised"] + result["removed"] + result["added"] >= 1

    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        active_deadlines = conn.execute(
            "select v.fact from public.memory_versions v"
            " join public.memory_entries e on e.id=v.entry_id"
            " where e.team_id=%s and v.is_active and v.fact ilike '%%december%%'",
            (TEAM_A,),
        ).fetchall()
        message_citations = conn.execute(
            "select count(*) from public.memory_citations c"
            " join public.memory_versions v on v.id = c.version_id"
            " where v.team_id=%s and c.source_kind='message'",
            (TEAM_A,),
        ).fetchone()[0]
        watermark = conn.execute(
            "select count(*) from public.memory_compilations"
            " where team_id=%s and chat_through is not null and status='done'",
            (TEAM_A,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert len(active_deadlines) <= 1, f"contradictory facts coexist: {active_deadlines}"
    assert message_citations >= 1, "chat compile produced no message citations"
    assert watermark == 1, "chat compilation did not record its watermark"


def test_compiled_facts_land_on_pages(seeded):
    compile_document(
        TEAM_A, DOC1,
        spotlight("Decision: we will use PostgreSQL. Alice owns the backend."),
    )
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        orphaned = conn.execute(
            "select count(*) from public.memory_entries e"
            " join public.memory_versions v on v.entry_id = e.id"
            " where e.team_id=%s and v.compilation_id is not null"
            " and e.page_id is null",
            (TEAM_A,),
        ).fetchone()[0]
        titles = [r[0] for r in conn.execute(
            "select title from public.memory_pages where team_id=%s", (TEAM_A,),
        ).fetchall()]
    finally:
        conn.close()
    assert orphaned == 0, "compile produced page-less entries"
    assert titles, "no wiki pages were created"
