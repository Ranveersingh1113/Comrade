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

Trigger: the worker loop calls sweep_chat_compiles() between queue drains, so
capture is ambient — nobody asks for it, it just keeps up with the room.
"""
from psycopg.types.json import Json

from pipeline.compiler import (
    apply_compilation, consolidate, extract_candidates,
)
from pipeline.parsers import spotlight
from pipeline.wiki import all_active_pages
from pipeline.worker import register
from shared.db import Role, connect, team_session

MIN_CHAT_MESSAGES = 5  # debounce: don't compile until this many new messages

_FETCH_COLUMNS = (
    "select m.id, p.display_name, m.body, m.created_at"
    " from public.messages m"
    " join public.threads th on th.id=m.thread_id and th.team_id=m.team_id"
    " join public.profiles p on p.id = m.sender_id"
    " where m.team_id = %s and th.visibility = 'team'"
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


def sweep_chat_compiles(min_messages: int = MIN_CHAT_MESSAGES) -> list[str]:
    """Enqueue a chat compile for every team with enough unswept group chatter.

    The scan crosses teams, so it is control-plane (admin), like the job claim.
    It is only a prefilter — enqueue_chat_compile re-checks the watermark and
    threshold authoritatively under the PIPELINE role, and its dedupe key makes
    a repeat sweep return the same job rather than a duplicate. Returns the job
    ids touched this pass.
    """
    with connect(Role.CONTROL) as conn:
        rows = conn.execute(
            # findings §3.2 calls the original "the worst query in the
            # codebase": a cross-team Seq Scan of `messages` with a CORRELATED
            # SUBQUERY in the filter, so the watermark lookup ran once per
            # candidate ROW. Measured on 6k messages: 6001 subplan executions,
            # 12,101 buffers, 22.9 ms — every 5 seconds, forever.
            #
            # Driving from `teams` instead inverts it. Teams are few, so the
            # watermark subquery runs once per TEAM; and because the inner
            # count carries a literal t.id, the partial index
            # idx_messages_group_human can serve it as a range scan. The
            # thread visibility is the canonical eligibility boundary. In the
            # steady state the tick actually sees — everything already swept —
            # that is a Bitmap Index Scan returning nothing.
            #
            # Same corpus, steady state: 0.097 ms. The CTE form I tried first
            # removed the subplan but still Seq Scanned, because a predicate
            # that depends on a joined row cannot become an index condition.
            "select t.id from public.teams t"
            " cross join lateral ("
            "   select count(*) as n from public.messages m"
            "   join public.threads th on th.id=m.thread_id and th.team_id=m.team_id"
            "    where m.team_id = t.id and th.visibility='team'"
            "      and m.sender_kind='user' and m.deleted_scope is null"
            "      and m.created_at > coalesce(("
            "            select max(c.chat_through)"
            "              from public.memory_compilations c"
            "             where c.team_id = t.id and c.chat_through is not null"
            "               and c.status='done'), '-infinity'::timestamptz)"
            " ) s where s.n >= %s",
            (min_messages,),
        ).fetchall()
    jobs = []
    for (team_id,) in rows:
        job_id = enqueue_chat_compile(str(team_id), min_messages)
        if job_id is not None:
            jobs.append(job_id)
    return jobs


def compile_messages(
    team_id: str, messages: list[dict], through, trigger: str = "scheduled"
) -> dict:
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
            trigger=trigger, chat_through=through,
        )


def handle_chat_compile_job(team_id: str, payload: dict) -> None:
    """Worker handler for 'compile_memory' jobs.

    `trigger` distinguishes the automatic sweep from a member asking for
    something to be remembered (§20.7.1). It is not bookkeeping: a fact a human
    explicitly marked is the highest-signal fact in the system, and a
    compilation indistinguishable from the sweep throws that away.
    """
    with team_session(Role.PIPELINE, team_id) as conn:
        messages = fetch_chat_messages_by_id(
            conn, team_id, payload.get("message_ids", [])
        )
    compile_messages(
        team_id, messages, payload.get("through"),
        trigger=payload.get("trigger", "scheduled"),
    )


register("compile_memory", handle_chat_compile_job)


def enqueue_remember(team_id: str, message_id: str) -> str:
    """Queue a compile of ONE message, now, because a member asked (§20.7.1).

    Deliberately the same job type and the same handler as the sweep. Members
    cannot write memory_* — that is comrade_pipeline's alone (§6.0) — and
    routing through the compiler is what keeps the datamarking guarantee: the
    text is spotlighted before it reaches a model, exactly as it would be if
    the sweep had picked it up.

    `through` is None ON PURPOSE. A chat compile normally stamps chat_through,
    and chat_watermark takes the max across done compilations — so stamping
    this one would advance the sweep past every message it has not read yet.
    Remembering one line would silently discard the conversation around it.
    """
    payload = {"message_ids": [str(message_id)], "through": None,
               "trigger": "on_demand"}
    dedupe_key = f"remember:{message_id}"
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
            # Already queued — a double tap, or two members marking the same
            # line. One request, one job.
            row = conn.execute(
                "select id from public.jobs where team_id=%s"
                " and job_type='compile_memory' and dedupe_key=%s",
                (team_id, dedupe_key),
            ).fetchone()
    return str(row[0])
