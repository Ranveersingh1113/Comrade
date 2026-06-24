"""Live smoke: compile a small document into memory facts + a diff card.
Makes real Gemini (extract) + embedding calls.

Run: uv run python scripts/smoke_pipeline.py
"""
import psycopg

from pipeline.compiler import compile_document
from pipeline.parsers import spotlight
from shared.config import settings
from tests._seed import TEAM_A, cleanup, seed

DOC_ID = "d0000000-0000-0000-0000-0000000000d1"
DOC_TEXT = (
    "Project brief: build a student attendance tracker.\n"
    "Deadline: final demo on Friday, December 18.\n"
    "Alice owns the backend API. Bob is responsible for the React frontend.\n"
    "We decided to drop the mobile app for now."
)


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def main():
    conn = _admin()
    with conn.cursor() as cur:
        cleanup(cur)
        seed(cur)
    conn.close()
    try:
        result = compile_document(TEAM_A, DOC_ID, spotlight(DOC_TEXT))
        print(f"[COMPILE] {result}")
        conn = _admin()
        facts = conn.execute(
            "select v.change_type, v.fact, c.excerpt"
            " from public.memory_versions v"
            " join public.memory_entries e on e.id = v.entry_id"
            " left join public.memory_citations c on c.version_id = v.id"
            " where e.team_id=%s and v.is_active and v.compilation_id is not null"
            " order by v.created_at",
            (TEAM_A,),
        ).fetchall()
        for change, fact, excerpt in facts:
            print(f"[{change.upper()}] {fact}   (cite: {excerpt})")
        card = conn.execute(
            "select body from public.messages where id=%s",
            (result["diff_message_id"],),
        ).fetchone()[0]
        print(f"[CARD] {card}")
        conn.close()
    finally:
        conn = _admin()
        with conn.cursor() as cur:
            cleanup(cur)
        conn.close()


if __name__ == "__main__":
    main()
