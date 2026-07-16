"""Live two-stage compile: real Gemini extract + consolidate + embeddings.

Skipped without a Gemini key. Model nondeterminism note: the revise assertion
tolerates 'revised' OR 'invalidated' on the target entry (both supersede);
what it must NOT be is an untouched old fact alongside a contradicting new one.
"""
import psycopg
import pytest

from pipeline.compiler import compile_document
from pipeline.parsers import spotlight
from shared.config import settings
from shared.db import Role, team_session
from shared.embeddings import embed_one
from tests._seed import TEAM_A

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


def test_embeddings_carry_provenance(seeded):
    compile_document(TEAM_A, DOC1, spotlight("Decision: we will use PostgreSQL."))
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        row = conn.execute(
            "select embedding_model, embedding_dim from public.memory_versions"
            " where team_id=%s and embedding is not null"
            " order by created_at desc limit 1",
            (TEAM_A,),
        ).fetchone()
    finally:
        conn.close()
    assert row == ("gemini-embedding-001", 1536)
