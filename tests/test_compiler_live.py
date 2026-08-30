"""Live two-stage compile: real Gemini extract + consolidate.

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

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not settings.gemini_api_key, reason="no GEMINI_API_KEY configured"
    ),
]

DOC1 = "d0000000-0000-0000-0000-0000000000d1"
DOC2 = "d0000000-0000-0000-0000-0000000000d2"


@pytest.fixture(autouse=True)
def _source_documents(seeded):
    """Citations must point at real same-team documents (integrity trigger,
    migration 20260719090000) — seed the rows the compiles will cite."""
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        for doc_id, name in ((DOC1, "plan.txt"), (DOC2, "update.txt")):
            conn.execute(
                "insert into public.documents (id, team_id, kind, filename)"
                " values (%s,%s,'text',%s) on conflict (id) do nothing",
                (doc_id, TEAM_A, name),
            )
        yield
    finally:
        conn.close()


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


def test_repo_activity_compiles_and_provenance_holds_against_a_real_model(
    seeded, capsys
):
    """A live repo compile, with the two artifacts §22.3 forbids sitting right
    next to the eligible ones: a Dependabot issue and an UNMERGED PR proposing
    a database switch that never happened. If either reached the model, the
    wiki would carry a machine's claim or an unverified proposal as a team
    decision — so this asserts on the compiled facts, not just on a return
    value."""
    from pipeline.chat import chat_watermark
    from pipeline.github import compile_github_activity, fetch_new_activity
    from shared.db import Role, team_session
    from tests.test_github_compile import _activity, _admin, _repo

    admin = _admin()
    try:
        repo = _repo(admin)
        _activity(
            admin, repo, "merge", author="maya", merged=True, number=41,
            title="Pool connections per role",
            body="Every agent turn opened one connection per operation."
                 " Each role now shares a pooled connection, fixed at 10.",
        )
        _activity(
            admin, repo, "review", author="arjun", number=41, state="approved",
            title="Pool connections per role",
            body="Checked that every scoping statement is transaction-scoped,"
                 " so a recycled connection cannot leak the previous team.",
        )
        _activity(
            admin, repo, "issue", author="maya", number=42,
            title="Consent tier T3 is unused",
            body="T3 is gone from the consent model. The tiers are T0 to T2.",
        )
        # forbidden: a machine's own account
        _activity(
            admin, repo, "issue", author="dependabot[bot]", bot=True, number=44,
            title="Bump pymupdf from 1.27.2 to 1.28.0",
            body="Bumps pymupdf to 1.28.0. Dependabot will resolve conflicts.",
        )
        # forbidden: nobody merged this, so nobody verified it
        _activity(
            admin, repo, "pr", author="maya", merged=False, state="open", number=43,
            title="Switch the wiki store to Neo4j",
            body="Replaces Postgres with Neo4j for the whole wiki.",
        )
    finally:
        admin.close()

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        rows = fetch_new_activity(conn, TEAM_A, None)
    assert len(rows) == 3, f"eligibility let the wrong rows through: {rows}"

    result = compile_github_activity(
        TEAM_A, rows, max(r["created_at"] for r in rows)
    )
    assert result["added"] >= 1

    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        facts = [r[0] for r in conn.execute(
            "select v.fact from public.memory_versions v"
            " where v.team_id=%s and v.is_active", (TEAM_A,),
        ).fetchall()]
        cited = conn.execute(
            "select c.source_kind, c.excerpt from public.memory_citations c"
            " join public.memory_versions v on v.id = c.version_id"
            " where v.team_id=%s and c.source_kind='github'", (TEAM_A,),
        ).fetchall()
    finally:
        conn.close()
    with capsys.disabled():
        print("\n--- live repo compile ---")
        print(f"result: {result}")
        for f in facts:
            print(f"  fact: {f}")
        for kind, excerpt in cited:
            print(f"  cite[{kind}]: {excerpt}")

    assert cited, "repo compile produced no github citations"
    blob = " ".join(facts).lower()
    assert "neo4j" not in blob, "an UNMERGED PR's proposal became a team fact"
    assert "dependabot" not in blob and "pymupdf" not in blob, (
        "a bot-authored issue became a team fact"
    )

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        assert chat_watermark(conn, TEAM_A) is None, (
            "a repo compile advanced the chat watermark"
        )


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
