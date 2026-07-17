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


DOC2_ID = "d0000000-0000-0000-0000-0000000000d2"
DOC2_TEXT = (
    "Update from today's meeting: the final demo moved to Monday, December 21.\n"
    "We are cancelling the React frontend — the demo will be CLI-only."
)


def main():
    conn = _admin()
    with conn.cursor() as cur:
        cleanup(cur)
        seed(cur)
    conn.close()
    try:
        r1 = compile_document(TEAM_A, DOC_ID, spotlight(DOC_TEXT))
        print(f"[COMPILE 1] {r1}")
        r2 = compile_document(TEAM_A, DOC2_ID, spotlight(DOC2_TEXT))
        print(f"[COMPILE 2] {r2}")
        conn = _admin()
        facts = conn.execute(
            "select coalesce(p.title,'Uncategorized'), v.change_type,"
            " v.is_active, v.fact"
            " from public.memory_versions v"
            " join public.memory_entries e on e.id = v.entry_id"
            " left join public.memory_pages p on p.id = e.page_id"
            " where e.team_id=%s and v.compilation_id is not null"
            " order by 1, v.created_at",
            (TEAM_A,),
        ).fetchall()
        for page, change, active, fact in facts:
            flag = "ACTIVE" if active else "closed"
            print(f"[{page:14s}|{change.upper():11s}|{flag}] {fact}")
        conn.close()
    finally:
        conn = _admin()
        with conn.cursor() as cur:
            cleanup(cur)
        conn.close()


if __name__ == "__main__":
    main()
