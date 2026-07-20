"""Chat -> memory: compile group-chat messages into wiki facts.

The capture side of the wiki loop (PromptQL's "action flows back to context"):
corrections and decisions stated in chat become cited, revertible facts via the
SAME compiler stages as documents — extraction is chat-tuned, consolidation and
apply are shared, and every fact cites the message it came from
(source_kind='message').

Hard invariants:
  - GROUP thread, human senders, undeleted messages only. Private threads
    never reach memory. AI messages are excluded (compiling our own diff
    cards would be circular).
  - The transcript is spotlighted before any LLM call — chat is THE
    untrusted input.
  - Debounce: enqueue_chat_compile only fires at >= min_messages new group
    messages since the last chat compilation's watermark (chat_through).

Trigger wiring (cron / agent capture tool) arrives with the event-bus slice;
until then enqueue_chat_compile is called explicitly (tests, smoke, future
runtime).
"""
from psycopg.types.json import Json

from pipeline.compiler import (
    apply_compilation, consolidate, extract_candidates,
)
from pipeline.parsers import spotlight
from pipeline.wiki import all_active_pages
from pipeline.worker import register
from shared.db import Role, team_session

MIN_CHAT_MESSAGES = 5  # debounce: don't compile until this many new messages

_FETCH_COLUMNS = (
    "select m.id, p.display_name, m.body, m.created_at"
    " from public.messages m"
    " join public.profiles p on p.id = m.sender_id"
    " where m.team_id = %s and m.thread_type = 'group'"
    " and m.sender_kind = 'user' and m.deleted_scope is null"
)


def fetch_new_chat_messages(conn, team_id: str, since) -> list[dict]:
    """Group/human/undeleted messages newer than the watermark, oldest first."""
    rows = conn.execute(
        _FETCH_COLUMNS + " and m.created_at > coalesce(%s, '-infinity'::timestamptz)"
        " order by m.created_at",
        (team_id, since),
    ).fetchall()
    return [
        {"id": str(r[0]), "sender": r[1], "text": r[2], "created_at": r[3]}
        for r in rows
    ]


def fetch_chat_messages_by_id(conn, team_id: str, message_ids: list[str]) -> list[dict]:
    """Re-fetch an enqueued batch by id (fresh state: deletions since enqueue
    drop out naturally), oldest first."""
    if not message_ids:
        return []
    rows = conn.execute(
        _FETCH_COLUMNS + " and m.id = any(%s::uuid[]) order by m.created_at",
        (team_id, message_ids),
    ).fetchall()
    return [
        {"id": str(r[0]), "sender": r[1], "text": r[2], "created_at": r[3]}
        for r in rows
    ]


def format_transcript(messages: list[dict]) -> str:
    """Numbered transcript lines: '[i] Name: text' — the numbering is what
    extraction's source_index refers back to."""
    return "\n".join(
        f"[{i}] {m['sender']}: {m['text']}" for i, m in enumerate(messages)
    )


def chat_watermark(conn, team_id: str):
    """Latest chat_through across done chat compilations (None if never ran)."""
    return conn.execute(
        "select max(chat_through) from public.memory_compilations"
        " where team_id = %s and chat_through is not null and status = 'done'",
        (team_id,),
    ).fetchone()[0]


def enqueue_chat_compile(
    team_id: str, min_messages: int = MIN_CHAT_MESSAGES
) -> str | None:
    """Debounced enqueue: if >= min_messages new group messages exist past the
    watermark, queue a compile_memory job carrying their ids + new watermark.
    Returns the job id, or None when below threshold."""
    with team_session(Role.PIPELINE, team_id) as conn:
        since = chat_watermark(conn, team_id)
        messages = fetch_new_chat_messages(conn, team_id, since)
    if len(messages) < min_messages:
        return None

    payload = {
        "message_ids": [m["id"] for m in messages],
        "through": max(m["created_at"] for m in messages).isoformat(),
    }
    dedupe_key = f"chat:{payload['through']}"
    with team_session(Role.PIPELINE, team_id) as conn:
        row = conn.execute(
            "insert into public.jobs (team_id, job_type, payload, dedupe_key)"
            " values (%s,'compile_memory',%s,%s)"
            " on conflict (team_id, job_type, dedupe_key)"
            " where dedupe_key is not null and status in ('pending','processing')"
            " do nothing returning id",
            (team_id, Json(payload), dedupe_key),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "select id from public.jobs where team_id=%s and job_type='compile_memory'"
                " and dedupe_key=%s",
                (team_id, dedupe_key),
            ).fetchone()
    return str(row[0])


def compile_messages(team_id: str, messages: list[dict], through) -> dict:
    """Two-stage compile of a chat batch: extract (chat prompt, per-line
    source_index) -> consolidate against the wiki -> apply with per-message
    citations. Always writes the compilation row, so the watermark advances
    even when the batch was pure chitchat (zero candidates)."""
    transcript = format_transcript(messages)
    marked = spotlight(transcript)
    candidates = extract_candidates(marked, kind="chat") if messages else []

    # Map each candidate's transcript line back to the message it cites;
    # unmappable indices degrade to "no citation", never to a wrong one.
    sources: list[tuple[str, str] | None] = []
    for cand in candidates:
        idx = cand.source_index
        if idx is not None and 0 <= idx < len(messages):
            sources.append(("message", messages[idx]["id"]))
        else:
            sources.append(None)

    with team_session(Role.PIPELINE, team_id) as conn:
        pages = all_active_pages(conn, team_id)

    decisions = (
        consolidate(candidates, pages, len(marked)) if candidates else []
    )

    with team_session(Role.PIPELINE, team_id) as conn:
        return apply_compilation(
            conn, team_id, candidates, decisions, sources,
            trigger="scheduled", chat_through=through,
        )


def handle_chat_compile_job(team_id: str, payload: dict) -> None:
    """Worker handler for 'compile_memory' jobs."""
    with team_session(Role.PIPELINE, team_id) as conn:
        messages = fetch_chat_messages_by_id(
            conn, team_id, payload.get("message_ids", [])
        )
    compile_messages(team_id, messages, payload.get("through"))


register("compile_memory", handle_chat_compile_job)
