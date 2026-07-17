"""Chat->memory: transcript, message citations, debounced enqueue (no LLM)."""
import psycopg

from pipeline.chat import (
    MIN_CHAT_MESSAGES, chat_watermark, enqueue_chat_compile,
    fetch_chat_messages_by_id, fetch_new_chat_messages, format_transcript,
)
from pipeline.compiler import Candidate, Decision, apply_compilation
from shared.config import settings
from shared.db import Role, team_session
from tests._seed import A1, A2, TEAM_A


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _post_group(cur, team_id, sender, body):
    return str(cur.execute(
        "insert into public.messages (team_id, thread_type, sender_kind,"
        " sender_id, body) values (%s,'group','user',%s,%s) returning id",
        (team_id, sender, body),
    ).fetchone()[0])


# ---------- pure ----------

def test_format_transcript_numbers_lines():
    msgs = [
        {"id": "m1", "sender": "Alice", "text": "deadline moved to Friday"},
        {"id": "m2", "sender": "Bob", "text": "ok noted"},
    ]
    out = format_transcript(msgs)
    assert out == "[0] Alice: deadline moved to Friday\n[1] Bob: ok noted"


# ---------- apply: per-message citations ----------

def test_apply_writes_message_citations(seeded):
    conn = _admin()
    try:
        with conn.cursor() as cur:
            msg_id = _post_group(cur, TEAM_A, A2, "the deadline moved to Friday")
    finally:
        conn.close()

    cands = [Candidate(text="Deadline is Friday", excerpt="moved to Friday")]
    decs = [Decision(candidate_index=0, action="add", page_title="Deadlines")]
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        result = apply_compilation(
            conn, TEAM_A, cands, decs, [("message", msg_id)],
            trigger="scheduled",
        )
    conn = _admin()
    try:
        kind, source_id, trigger = conn.execute(
            "select c.source_kind, c.source_id, mc.trigger"
            " from public.memory_citations c"
            " join public.memory_versions v on v.id = c.version_id"
            " join public.memory_compilations mc on mc.id = v.compilation_id"
            " where v.compilation_id = %s",
            (result["compilation_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert (kind, str(source_id), trigger) == ("message", msg_id, "scheduled")


def test_apply_none_source_skips_citation(seeded):
    cands = [Candidate(text="Uncited fact", excerpt="some excerpt")]
    decs = [Decision(candidate_index=0, action="add")]
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        result = apply_compilation(conn, TEAM_A, cands, decs, [None])
    conn = _admin()
    try:
        n = conn.execute(
            "select count(*) from public.memory_citations c"
            " join public.memory_versions v on v.id = c.version_id"
            " where v.compilation_id = %s",
            (result["compilation_id"],),
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 0


# ---------- fetch + debounce ----------

def test_fetch_excludes_ai_deleted_and_private(seeded):
    conn = _admin()
    try:
        with conn.cursor() as cur:
            keep = _post_group(cur, TEAM_A, A2, "real message")
            cur.execute(
                "insert into public.messages (team_id, thread_type, sender_kind,"
                " body) values (%s,'group','ai','Memory updated — 1 added.')",
                (TEAM_A,),
            )
            deleted = _post_group(cur, TEAM_A, A2, "oops")
            cur.execute(
                "update public.messages set deleted_scope='everyone' where id=%s",
                (deleted,),
            )
            cur.execute(
                "insert into public.messages (team_id, thread_type,"
                " thread_owner_id, sender_kind, sender_id, body)"
                " values (%s,'private',%s,'user',%s,'private note')",
                (TEAM_A, A1, A1),
            )
    finally:
        conn.close()
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        texts = {m["text"] for m in fetch_new_chat_messages(conn, TEAM_A, None)}
        by_id = fetch_chat_messages_by_id(conn, TEAM_A, [keep, deleted])
    assert "real message" in texts
    assert "oops" not in texts and "private note" not in texts
    assert not any("Memory updated" in t for t in texts)
    assert [m["text"] for m in by_id] == ["real message"]  # deleted dropped on re-fetch


def test_enqueue_debounces_below_threshold(seeded):
    # seed() provides exactly 1 group user message -> below MIN_CHAT_MESSAGES
    assert enqueue_chat_compile(TEAM_A) is None


def test_enqueue_fires_at_threshold_and_advances_watermark(seeded):
    conn = _admin()
    try:
        with conn.cursor() as cur:
            for i in range(MIN_CHAT_MESSAGES - 1):  # +1 from seed = threshold
                _post_group(cur, TEAM_A, A2, f"update {i}: backend on track")
    finally:
        conn.close()

    job_id = enqueue_chat_compile(TEAM_A)
    assert job_id is not None

    conn = _admin()
    try:
        payload = conn.execute(
            "select payload from public.jobs where id=%s", (job_id,),
        ).fetchone()[0]
        assert len(payload["message_ids"]) == MIN_CHAT_MESSAGES
        # simulate the compile having run: record the watermark
        conn.execute(
            "insert into public.memory_compilations (team_id, trigger, status,"
            " chat_through) values (%s,'scheduled','done',%s)",
            (TEAM_A, payload["through"]),
        )
    finally:
        conn.close()

    # nothing new past the watermark -> debounced again
    assert enqueue_chat_compile(TEAM_A) is None

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        assert chat_watermark(conn, TEAM_A) is not None
