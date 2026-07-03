"""Read-side helpers for the consolidation stage.

All queries run on a caller-provided team_session(Role.PIPELINE, team_id)
connection, so RLS confines them to the one team; team_id appears in SQL only
as a redundant belt-and-braces filter.
"""


def vec_literal(vec: list[float]) -> str:
    """Render a Python vector as a pgvector literal."""
    return "[" + ",".join(repr(x) for x in vec) + "]"


def count_active_facts(conn, team_id: str) -> int:
    """Number of active, unarchived facts for the team (fast-path gate)."""
    return conn.execute(
        "select count(*) from public.memory_entries e"
        " join public.memory_versions v on v.entry_id = e.id and v.is_active"
        " where e.team_id = %s and not e.archived",
        (team_id,),
    ).fetchone()[0]


def all_active_facts(conn, team_id: str) -> list[dict]:
    """Every active fact — the fast-path neighbor set for small teams."""
    rows = conn.execute(
        "select e.id, v.fact from public.memory_entries e"
        " join public.memory_versions v on v.entry_id = e.id and v.is_active"
        " where e.team_id = %s and not e.archived order by v.created_at",
        (team_id,),
    ).fetchall()
    return [{"entry_id": str(r[0]), "text": r[1]} for r in rows]


def find_similar_facts(
    conn, team_id: str, query_vec: list[float], k: int
) -> list[dict]:
    """Top-k active facts by cosine similarity to query_vec.

    Rows without embeddings are excluded — they can't rank, so past the fast
    path they are unreachable for revision (acceptable: only pre-pipeline seed
    rows lack embeddings).
    """
    rows = conn.execute(
        "select e.id, v.fact from public.memory_entries e"
        " join public.memory_versions v on v.entry_id = e.id and v.is_active"
        " where e.team_id = %s and not e.archived and v.embedding is not null"
        " order by v.embedding <=> %s::vector limit %s",
        (team_id, vec_literal(query_vec), k),
    ).fetchall()
    return [{"entry_id": str(r[0]), "text": r[1]} for r in rows]
