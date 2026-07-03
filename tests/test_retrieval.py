"""pgvector retrieval helpers for the consolidation stage (DB, no LLM)."""
import psycopg

from pipeline.retrieval import (
    all_active_facts, count_active_facts, find_similar_facts, vec_literal,
)
from shared.config import settings
from shared.db import Role, team_session
from tests._seed import TEAM_A, TEAM_B


def _unit(i: int) -> list[float]:
    """1536-dim unit vector along axis i."""
    v = [0.0] * 1536
    v[i] = 1.0
    return v


def _seed_fact(cur, team_id, text, vec=None):
    entry_id = cur.execute(
        "insert into public.memory_entries (team_id) values (%s) returning id",
        (team_id,),
    ).fetchone()[0]
    if vec is None:
        cur.execute(
            "insert into public.memory_versions (entry_id, team_id, fact,"
            " change_type) values (%s,%s,%s,'added')",
            (entry_id, team_id, text),
        )
    else:
        cur.execute(
            "insert into public.memory_versions (entry_id, team_id, fact,"
            " change_type, embedding) values (%s,%s,%s,'added',%s::vector)",
            (entry_id, team_id, text, vec_literal(vec)),
        )
    return str(entry_id)


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def test_count_and_all_scoped_to_team(seeded):
    conn = _admin()
    try:
        with conn.cursor() as cur:
            _seed_fact(cur, TEAM_A, "A-fact", _unit(0))
            _seed_fact(cur, TEAM_B, "B-fact", _unit(1))
    finally:
        conn.close()
    with team_session(Role.PIPELINE, TEAM_A) as s:
        # seed() already inserts 1 embeddingless fact for TEAM_A
        assert count_active_facts(s, TEAM_A) == 2
        texts = {f["text"] for f in all_active_facts(s, TEAM_A)}
    assert "A-fact" in texts and "B-fact" not in texts


def test_find_similar_orders_by_cosine_and_skips_null_embeddings(seeded):
    conn = _admin()
    try:
        with conn.cursor() as cur:
            near = _seed_fact(cur, TEAM_A, "near", _unit(0))
            _seed_fact(cur, TEAM_A, "far", _unit(1))
            _seed_fact(cur, TEAM_A, "no-embedding")  # must never be returned
    finally:
        conn.close()
    with team_session(Role.PIPELINE, TEAM_A) as s:
        got = find_similar_facts(s, TEAM_A, _unit(0), k=2)
    assert [g["text"] for g in got] == ["near", "far"]
    assert got[0]["entry_id"] == near


def test_find_similar_respects_team_boundary(seeded):
    conn = _admin()
    try:
        with conn.cursor() as cur:
            _seed_fact(cur, TEAM_B, "B-near", _unit(0))
    finally:
        conn.close()
    with team_session(Role.PIPELINE, TEAM_A) as s:
        got = find_similar_facts(s, TEAM_A, _unit(0), k=5)
    assert all(g["text"] != "B-near" for g in got)
