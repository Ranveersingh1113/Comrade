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
    apply_compilation, bind_to_seen_versions, consolidate, extract_candidates,
)
from pipeline.parsers import spotlight
from pipeline.wiki import all_active_pages
import datetime

from pipeline.worker import register
from shared.db import Role, connect, team_session

MIN_CHAT_MESSAGES = 5  # debounce: don't compile until this many new messages

#: 🔴 Count was the ONLY trigger. A team that made one important decision and
#: then went quiet never reached five messages, so the decision was never
#: captured — and "we decided X" is exactly the kind of thing a team says once.
#: Age is the second trigger, and it is a floor rather than a bypass: a
#: conversation still being typed should not be compiled a sentence at a time.
MAX_CAPTURE_AGE = datetime.timedelta(minutes=30)

#: 🔴 A batch was every message past the watermark, unbounded, so a team coming
#: back to a fortnight of backlog produced one enormous transcript in one
#: enormous model call. Bounded by records AND by characters, because fifty
#: pasted stack traces is not the same amount of work as fifty "ok"s.
MAX_CHAT_BATCH = 60
MAX_CHAT_BATCH_CHARS = 24_000

#: How far behind now() capture stays.
#:
#: 🔴 A row committed AFTER the watermark snapshot but stamped BEFORE it sits
#: below the watermark forever — a transaction that began earlier and committed
#: later is invisible to the reader that already moved past its timestamp.
#: Staying a little behind the clock lets in-flight writes land first.
CAPTURE_LAG_SECONDS = 5

#: The lowest possible uuid, so a null watermark id still forms a valid keyset
#: comparison rather than needing a second query shape.
_MIN_UUID = "00000000-0000-0000-0000-000000000000"

#: Was this line addressed to Comrade?
#:
#: 🔴 Nothing recorded it, so "@comrade investigate switching to Postgres" and
#: "we are switching to Postgres" reached the model as the same kind of
#: sentence — and a request to look into an option could be compiled into the
#: wiki as a decision the team had taken.
#:
#: Two signals, because neither alone is enough. The run is authoritative when
#: there is one; the mention catches the messages that never started a run at
#: all — a busy room refuses some, and a refused turn is still a question.
#: On the MESSAGE, not derived from agent_runs at read time: that table
#: deliberately has no broad grant (findings §4.1 — it holds private prompts
#: and tool results), and widening it so the compiler could ask one boolean
#: question would trade a real privacy boundary for a convenience. The
#: mention stays as a second signal, for messages written before the column
#: existed and for anything that reaches the table another way.
_TO_AGENT = " (m.to_agent or m.body ~* '@[[:space:]]*comrade')"

_FETCH_COLUMNS = (
    "select m.id, p.display_name, m.body, m.created_at, th.id, th.title,"
    + _TO_AGENT
    + " from public.messages m"
    " join public.threads th on th.id=m.thread_id and th.team_id=m.team_id"
    " join public.profiles p on p.id = m.sender_id"
    " where m.team_id = %s and th.visibility = 'team'"
    " and m.sender_kind = 'user' and m.deleted_scope is null"
)


def _row(r) -> dict:
    return {
        "id": str(r[0]), "sender": r[1], "text": r[2], "created_at": r[3],
        "thread_id": str(r[4]), "thread_title": r[5], "to_agent": bool(r[6]),
    }


def fetch_new_chat_messages(
    conn, team_id: str, since, since_id: str | None = None,
    limit: int = MAX_CHAT_BATCH,
) -> list[dict]:
    """Group/human/undeleted messages past the watermark, oldest first.

    The boundary is the KEYSET `(created_at, id)`, not `created_at` alone:
    timestamps are not unique — two people answering at once, or one
    transaction inserting several, share them routinely — and a bare `>` on the
    timestamp skips whichever of a tied pair the watermark landed on, forever.
    """
    rows = conn.execute(
        _FETCH_COLUMNS
        + " and (m.created_at, m.id) >"
          " (coalesce(%s, '-infinity'::timestamptz), coalesce(%s, %s)::uuid)"
        + f" and m.created_at <= now() - interval '{CAPTURE_LAG_SECONDS} seconds'"
        + " order by m.created_at, m.id limit %s",
        (team_id, since, since_id, _MIN_UUID, limit),
    ).fetchall()
    return [_row(r) for r in rows]


def bound_batch(messages: list[dict], max_chars: int = MAX_CHAT_BATCH_CHARS) -> list[dict]:
    """Trim a batch to what one model call should carry.

    Never returns empty for a non-empty input: one message longer than the
    whole budget still has to be compiled, or it blocks the watermark for good.
    """
    kept: list[dict] = []
    used = 0
    for message in messages:
        size = len(message["text"] or "")
        if kept and used + size > max_chars:
            break
        kept.append(message)
        used += size
    return kept


def group_by_thread(messages: list[dict]) -> list[dict]:
    """Same messages, one conversation at a time.

    🔴 Every team-visible thread was ordered together by time, so two unrelated
    conversations reached the model as one exchange — and a model asked to
    extract decisions from that will happily invent the connection between
    them. Threads appear in the order they first appear, so the batch boundary
    is unchanged; only the presentation is grouped.
    """
    groups: dict[str, list[dict]] = {}
    for message in messages:
        groups.setdefault(message.get("thread_id") or "", []).append(message)
    return [message for group in groups.values() for message in group]


def fetch_chat_messages_by_id(conn, team_id: str, message_ids: list[str]) -> list[dict]:
    """Re-fetch an enqueued batch by id (fresh state: deletions since enqueue
    drop out naturally), oldest first."""
    if not message_ids:
        return []
    rows = conn.execute(
        _FETCH_COLUMNS + " and m.id = any(%s::uuid[]) order by m.created_at, m.id",
        (team_id, message_ids),
    ).fetchall()
    return [_row(r) for r in rows]


def format_transcript(messages: list[dict], trigger: str = "scheduled") -> str:
    """Numbered transcript lines: '[i] Name: text'.

    The numbering is what extraction's source_index refers back to, so it stays
    global and in the order given. A thread header appears whenever the
    conversation changes, WITHOUT consuming an index — the reader needs to know
    these are separate conversations; the citation map does not change.

    A line addressed to Comrade is written `Name -> Comrade`, because a request
    to investigate an option and a decision to take it are different things and
    the sentences look alike. That is CONTEXT for the judgement, not a veto on
    it: a decision announced to Comrade is still a decision.
    """
    lines: list[str] = []
    if trigger == "on_demand":
        # A member pointed at this and asked for it to be kept. The codebase
        # already calls that the highest-signal fact in the system; it had
        # never actually reached the model.
        lines.append(
            "--- a member explicitly asked for this to be remembered ---"
        )
    current: str | None = None
    for i, m in enumerate(messages):
        thread = m.get("thread_title")
        if thread and thread != current:
            current = thread
            lines.append(f"--- {thread} ---")
        who = f"{m['sender']} -> Comrade" if m.get("to_agent") else m["sender"]
        lines.append(f"[{i}] {who}: {m['text']}")
    return "\n".join(lines)


def chat_watermark(conn, team_id: str):
    """Latest chat_through across done chat compilations (None if never ran)."""
    return conn.execute(
        "select max(chat_through) from public.memory_compilations"
        " where team_id = %s and chat_through is not null and status = 'done'",
        (team_id,),
    ).fetchone()[0]


def chat_keyset(conn, team_id: str) -> tuple:
    """The (timestamp, id) the next capture resumes from.

    The id belongs to the row that WAS the boundary, so a tie at that timestamp
    resumes after it rather than skipping its neighbours.
    """
    row = conn.execute(
        "select chat_through, chat_through_id from public.memory_compilations"
        " where team_id = %s and chat_through is not null and status = 'done'"
        " order by chat_through desc, chat_through_id desc nulls last limit 1",
        (team_id,),
    ).fetchone()
    return (row[0], str(row[1]) if row and row[1] else None) if row else (None, None)


def enqueue_chat_compile(
    team_id: str, min_messages: int = MIN_CHAT_MESSAGES
) -> str | None:
    """Enqueue a bounded batch when there is enough to say, or it has waited
    long enough. Returns the job id, or None when there is nothing to do yet."""
    with team_session(Role.PIPELINE, team_id) as conn:
        since, since_id = chat_keyset(conn, team_id)
        messages = fetch_new_chat_messages(conn, team_id, since, since_id)
    if not messages:
        return None
    aged = (
        datetime.datetime.now(datetime.timezone.utc) - messages[0]["created_at"]
        >= MAX_CAPTURE_AGE
    )
    if len(messages) < min_messages and not aged:
        return None

    batch = bound_batch(messages)
    last = batch[-1]
    payload = {
        "message_ids": [m["id"] for m in batch],
        "through": last["created_at"].isoformat(),
        "through_id": last["id"],
    }
    # Keyed on the boundary ROW, not the timestamp: two batches can share a
    # timestamp, and keying on it alone made the second one look like a
    # duplicate of the first and vanish.
    dedupe_key = f"chat:{payload['through']}:{last['id']}"
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


#: What capture will actually look at, as SQL.
#:
#: 🔴 `/metrics` had its own idea of this and compared EVERY message to the
#: compilation cursor — private threads, the agent's own messages, deleted
#: ones. A team whose conversation is entirely private reported a steadily
#: growing compiler backlog that no amount of working pipeline could clear,
#: because there was nothing to capture. Two definitions of the same word is
#: how a metric ends up measuring something nobody asked about.
#:
#: Expects `m` (messages) and `th` (threads) in scope.
CAPTURABLE_SQL = (
    " th.visibility='team'"
    " and m.sender_kind='user'"
    " and m.deleted_scope is null"
)

#: What capture has NOT taken yet, as SQL.
#:
#: 🔴 (fix.md F15) This was a timestamp comparison — `m.created_at >
#: max(chat_through)` — while the bounded fetch resumes from the COMPOUND
#: cursor `(created_at, id)` that `chat_keyset` returns. Timestamps are not
#: unique, and a batch boundary can fall in the middle of a group sharing one.
#: The fetch would correctly take the rest of that group on the next run; the
#: sweep, comparing timestamps only, excluded every row AT the boundary
#: timestamp and so never asked for another job. Those messages waited for
#: unrelated later chatter to arrive, and on a quiet team that is forever.
#:
#: The same ordering, in candidate discovery and in the fetch, or the two
#: disagree about which rows still exist.
#:
#: `status='done'` matters too: a compilation still running, or one that
#: failed, has captured nothing, and treating its cursor as progress hides a
#: real backlog.
#:
#: Expects `m` (messages) in scope and a `{team}` expression.
UNCAPTURED_SQL = (
    " (m.created_at, m.id) > coalesce("
    "   (select (c.chat_through, coalesce(c.chat_through_id,"
    "            '00000000-0000-0000-0000-000000000000'::uuid))"
    "      from public.memory_compilations c"
    "     where c.team_id = {team} and c.chat_through is not null"
    "       and c.status = 'done'"
    "     order by c.chat_through desc, c.chat_through_id desc nulls last"
    "     limit 1),"
    "   ('-infinity'::timestamptz,"
    "    '00000000-0000-0000-0000-000000000000'::uuid))"
)


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
            "   select count(*) as n, min(m.created_at) as oldest"
            "     from public.messages m"
            "   join public.threads th on th.id=m.thread_id and th.team_id=m.team_id"
            "    where m.team_id = t.id and" + CAPTURABLE_SQL +
            "      and" + UNCAPTURED_SQL.format(team="t.id") +
            # The prefilter has to agree with the authoritative check in
            # enqueue_chat_compile, or the sweep never calls it and the age
            # rule is unreachable.
            " ) s where s.n >= %s or s.oldest <= now() - %s::interval",
            (min_messages, f"{int(MAX_CAPTURE_AGE.total_seconds())} seconds"),
        ).fetchall()
    jobs = []
    for (team_id,) in rows:
        job_id = enqueue_chat_compile(str(team_id), min_messages)
        if job_id is not None:
            jobs.append(job_id)
    return jobs


def compile_messages(
    team_id: str, messages: list[dict], through, trigger: str = "scheduled",
    through_id: str | None = None,
) -> dict:
    """Two-stage compile of a chat batch: extract (chat prompt, per-line
    source_index) -> consolidate against the wiki -> apply with per-message
    citations. Always writes the compilation row, so the watermark advances
    even when the batch was pure chitchat (zero candidates)."""
    # Grouped BEFORE the transcript is numbered, so source_index and the
    # citation map below refer to the same order the model was shown.
    messages = group_by_thread(messages)
    transcript = format_transcript(messages, trigger=trigger)
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
    # Bind each revision to the version this snapshot showed, so a second
    # compile cannot erase a first one it never saw.

    with team_session(Role.PIPELINE, team_id) as conn:
        return apply_compilation(
            conn, team_id, candidates, decisions, sources,
            trigger=trigger, chat_through=through, chat_through_id=through_id,
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
        through_id=payload.get("through_id"),
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
