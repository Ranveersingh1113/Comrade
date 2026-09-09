"""Replay a thread's prior turns from `messages` into ADK content.

Why not ADK's DatabaseSessionService: it needs SQLAlchemy (a second
data-access stack beside this project's raw psycopg3 + hand-written
migrations), and it creates UNQUALIFIED `sessions` / `events` / `app_states` /
`user_states` tables — they land in `public` beside the RLS tables with no RLS
and no team_id, and Supabase grants anon/authenticated full CRUD on new tables
by default. This phase just closed two private-thread leaks of exactly that
shape; four more such tables would undo the work.

`messages` is already the documented source of truth, already holds every turn
of both thread kinds, already has per-thread RLS, and is already read as the
requesting member (findings §4.1).

Scoping note: a member can read more than one visible thread. `thread_id` is
therefore an explicit scope as well as an RLS-authorized one.
"""
import logging
from google.genai import types

from shared.db import Role, team_session, user_session

logger = logging.getLogger(__name__)



# Newest-first with an explicit LIMIT (so the DB does the capping), reversed
# below into chronological order. `id is distinct from %s` also holds when the
# exclusion is NULL, so one statement covers both callers.
_SQL = (
    "select m.sender_kind, m.body, p.display_name, t.visibility,"
    " (select count(*) > 1 from public.thread_participants tp"
    "  where tp.thread_id = t.id)"
    " from public.messages m"
    " join public.threads t on t.id = m.thread_id and t.team_id = m.team_id"
    " left join public.profiles p on p.id = m.sender_id"
    " where m.team_id = %s and m.thread_id = %s::uuid"
    " and m.deleted_scope is null"
    " and m.id is distinct from %s::uuid"
    # 🔴 (fix.md F16) Everything since the summary, not just the last N.
    #
    # The summary advances in jumps: compaction waits for
    # MIN_COMPACT_MESSAGES older than the KEEP_RECENT_MESSAGES it will not
    # touch. The replay showed `agent_history_turns`. Between the two, up to
    # 39 messages were in NEITHER — with a summary through message 40 and a
    # thread at 61, the turn replayed 42-61 and message 41 was invisible.
    #
    # T19 exists because a constraint stated a hundred messages ago was lost.
    # It fixed the far end and left a moving hole just behind the replay
    # window, which is where a constraint stated ten minutes ago lives.
    #
    # A null cursor means no summary, and every row passes — the `limit`
    # below is then the only bound, which is the original behaviour.
    " and (%s::timestamptz is null"
    "      or (m.created_at, m.id) > (%s::timestamptz, %s::uuid))"
    " order by m.created_at desc, m.id desc limit %s"
)


#: The most messages a turn will replay, however far behind compaction is.
#:
#: The unsummarised tail is normally at most
#: MIN_COMPACT_MESSAGES + KEEP_RECENT_MESSAGES, because compaction closes it.
#: If compaction STOPS — a broken worker, a model outage — the tail grows
#: without limit, and an unbounded tail is an unbounded prompt on every turn.
#: Capping it reintroduces a gap in exactly that case, which is the lesser of
#: the two failures and a loud one: the log says so.
MAX_REPLAY_MESSAGES = 200


def recent_turns(
    team_id: str,
    requester_id: str,
    thread_id: str,
    limit: int,
    exclude_message_id: str | None = None,
    since: tuple | None = None,
) -> list[types.Content]:
    """This thread's conversation since the summary, oldest first.

    `since` is the summary's compound cursor `(timestamp, id)`, and passing it
    at all — including as `(None, None)` — asks for everything not yet
    summarised. Everything after the cursor is replayed, so the summary and the
    replay MEET rather than leaving a hole between them, and a thread with no
    summary is treated as entirely unsummarised rather than trimmed to `limit`
    (fix.md F16, F45). `limit` governs callers that want a window instead.

    exclude_message_id drops the message this turn is about: server/app.py
    persists the member's message BEFORE the runtime runs, so without it the
    model would receive the current question twice.

    Shared-thread bodies carry their sender's name — ADK's user role alone
    would merge several people into one voice.
    """
    if limit <= 0:
        return []
    through, through_id = since if since else (None, None)
    if through is not None and through_id is None:
        # Half a cursor is not a cursor: without the id a tie at the boundary
        # either repeats or vanishes. Fall back to the window rather than
        # guess.
        through = None
    # 🔴 (fix.md F45) Bound on whether the caller ASKED for the unsummarised
    # range, not on whether a summary happens to exist yet.
    #
    # This was `MAX_REPLAY_MESSAGES if through is not None else limit`, and
    # `through` is null until the first compaction — which waits for
    # MIN_COMPACT_MESSAGES (40) older than the KEEP_RECENT_MESSAGES (20) it
    # will not touch, so the first summary arrives at message 60. Until then a
    # thread replayed its last `limit` (20) messages and nothing else: a
    # constraint stated in message 1 was invisible from message 21, for forty
    # messages, on every new thread. F16 fixed the gap BETWEEN compactions and
    # left the one before the first.
    #
    # `since` given at all means "everything not yet summarised"; the empty
    # cursor is simply the case where that is the whole thread. `limit` still
    # governs callers that ask for a window instead.
    bound = MAX_REPLAY_MESSAGES if since is not None else limit
    with user_session(requester_id) as conn:
        rows = conn.execute(
            _SQL,
            (team_id, thread_id, exclude_message_id, through, through, through_id,
             bound),
        ).fetchall()
    if since is not None and len(rows) >= MAX_REPLAY_MESSAGES:
        logger.warning(
            "thread %s has %d+ unsummarised messages; replay is capped and"
            " compaction is behind", thread_id, MAX_REPLAY_MESSAGES,
        )
    return [
        types.Content(
            role="user" if kind == "user" else "model",
            parts=[types.Part(
                text=f"{name}: {body}"
                if kind == "user" and name and (visibility == "team" or shared)
                else body
            )],
        )
        for kind, body, name, visibility, shared in reversed(rows)
    ]


def steering_messages(
    team_id: str, requester_id: str, thread_id: str, run_id: str, seen: list[str],
) -> list[tuple[str, str | None, str]]:
    """Participant messages added after a run started and not yet shown to it.

    Returns (message id, sender name or None, body).

    🔴 The name is new. Steering used to arrive as "New participant message",
    unattributed, so in a room where anyone can redirect the work a teammate's
    instruction was indistinguishable from the requester's own. The model could
    not weigh who was asking — and the run's identity and approval ownership
    stay with the ORIGINAL requester regardless, which is precisely why the
    difference has to be visible in the text rather than implied by it.

    Attributed on the same rule `recent_turns` uses: a thread with one
    participant has nobody to tell apart.
    """
    # `agent_runs` intentionally has no authenticated grant: it contains private
    # prompts and tool results. Read only this run's boundary metadata as the
    # team-scoped worker, then still read message bodies as the requester.
    with team_session(Role.AGENT, team_id) as conn:
        run = conn.execute(
            "select input_message_id, created_at from public.agent_runs"
            " where id=%s and thread_id=%s",
            (run_id, thread_id),
        ).fetchone()
    if run is None:
        return []
    input_message_id, started_at = run
    with user_session(requester_id) as conn:
        rows = conn.execute(
            "select m.id, m.body, p.display_name, t.visibility,"
            " (select count(*) > 1 from public.thread_participants tp"
            "  where tp.thread_id = t.id)"
            " from public.messages m"
            " join public.threads t on t.id = m.thread_id and t.team_id = m.team_id"
            " left join public.profiles p on p.id = m.sender_id"
            " where m.team_id=%s and m.thread_id=%s and m.sender_kind='user'"
            " and m.id is distinct from %s::uuid"
            " and m.created_at >= %s"
            " and not (m.id = any(%s::uuid[]))"
            " order by m.created_at, m.id",
            (team_id, thread_id, input_message_id, started_at, seen),
        ).fetchall()
    return [
        (
            str(message_id),
            name if name and (visibility == "team" or shared) else None,
            body,
        )
        for message_id, body, name, visibility, shared in rows
    ]


# ---------------------------------------------------------------------------
# Thread working state (T19)
# ---------------------------------------------------------------------------
#
# 🔴 The agent's memory of a thread was the last `agent_history_turns` messages
# and nothing else. A constraint stated a hundred messages ago — "we are not
# touching the vendored fork", "the customer is still on Postgres 14" — was
# invisible to every later turn, so the agent would cheerfully propose the
# thing the team had already ruled out and the member had to say it again.
# Nothing survived a worker restart either: whatever the turn had worked out
# lived in a prompt that no longer existed.

#: How many recent messages compaction refuses to touch.
#:
#: Summarising the last thing somebody said, while they are still saying
#: things, replaces the exact words with a paraphrase of them at the worst
#: possible moment. Only an ACKNOWLEDGED range is compacted.
KEEP_RECENT_MESSAGES = 20

_STATE_COLUMNS = (
    "summary, summary_through, summary_through_id, pins, open_questions,"
    " plan_version, workspace_revision, pending"
)

_EMPTY_STATE = {
    "summary": "", "summary_through": None, "summary_through_id": None,
    "pins": [], "open_questions": [], "plan_version": None,
    "workspace_revision": None, "pending": {},
}


def replay_for_turn(
    team_id: str,
    requester_id: str,
    thread_id: str,
    limit: int,
    exclude_message_id: str | None = None,
) -> list[types.Content]:
    """The conversation a turn should see: everything since the summary.

    🔴 (fix.md F16) The runtime read the last `limit` messages and the summary
    covered an old prefix, and NOTHING made the two meet. Compaction advances
    the summary in jumps of MIN_COMPACT_MESSAGES, so up to 39 messages sat in
    neither window — with a summary through message 40 and a thread at 61, the
    turn replayed 42-61 and message 41 was invisible.

    Composed here rather than at the call site so there is ONE answer to "what
    does a turn see". Two callers assembling it separately is how the summary
    and the replay came to disagree in the first place.
    """
    state = working_state(team_id, thread_id)
    return recent_turns(
        team_id, requester_id, thread_id, limit, exclude_message_id,
        (state.get("summary_through"), state.get("summary_through_id")),
    )


def working_state(team_id: str, thread_id: str) -> dict:
    """What this thread has established, or empty defaults when it has none.

    Empty defaults rather than None: every caller wants "the pins" and "the
    summary", and a thread nobody has compacted yet has an empty list of pins
    rather than an absence of the concept.
    """
    with team_session(Role.AGENT, team_id) as conn:
        row = conn.execute(
            f"select {_STATE_COLUMNS} from public.thread_working_state"
            " where thread_id=%s",
            (thread_id,),
        ).fetchone()
    if row is None:
        return dict(_EMPTY_STATE)
    return {
        "summary": row[0] or "", "summary_through": row[1],
        "summary_through_id": str(row[2]) if row[2] else None,
        "pins": row[3] or [], "open_questions": row[4] or [],
        "plan_version": row[5], "workspace_revision": row[6],
        "pending": row[7] or {},
    }


def _ensure_state(conn, team_id: str, thread_id: str) -> None:
    conn.execute(
        "insert into public.thread_working_state (thread_id, team_id)"
        " values (%s,%s) on conflict (thread_id) do nothing",
        (thread_id, team_id),
    )


def pin_constraint(
    team_id: str, thread_id: str, text: str, *, added_by: str,
    source_message_id: str | None = None,
) -> None:
    """Record something this thread must not lose.

    Appended to `pins`, NOT folded into the summary. A rolling summary is
    rewritten by a model every time it grows, and prose gets paraphrased a
    little each round until it means something else. A constraint the team
    stated is not a thing to paraphrase.
    """
    from psycopg.types.json import Json

    entry = {
        "text": text, "added_by": added_by,
        "source_message_id": source_message_id,
    }
    with team_session(Role.AGENT, team_id) as conn:
        _ensure_state(conn, team_id, thread_id)
        conn.execute(
            "update public.thread_working_state"
            " set pins = pins || %s::jsonb, updated_at = now()"
            " where thread_id=%s",
            (Json([entry]), thread_id),
        )


def set_summary(
    team_id: str, thread_id: str, summary: str, *, through, through_id,
) -> None:
    """Replace the rolling summary and advance its cursor together.

    One statement, because a summary that covers a range while the cursor says
    otherwise is worse than either alone: the next compaction either re-reads
    what is already summarised or skips what is not.
    """
    with team_session(Role.AGENT, team_id) as conn:
        _ensure_state(conn, team_id, thread_id)
        conn.execute(
            "update public.thread_working_state set summary=%s,"
            " summary_through=%s, summary_through_id=%s, updated_at=now()"
            " where thread_id=%s",
            (summary, through, through_id, thread_id),
        )


_COMPACT_SQL = (
    "select m.id, m.body, p.display_name, m.created_at"
    " from public.messages m"
    " left join public.profiles p on p.id = m.sender_id"
    " where m.team_id=%s and m.thread_id=%s and m.deleted_scope is null"
    "   and (m.created_at, m.id) >"
    "       (coalesce(%s, '-infinity'::timestamptz),"
    "        coalesce(%s, '00000000-0000-0000-0000-000000000000')::uuid)"
    " order by m.created_at, m.id"
)


def compactable_range(
    team_id: str, requester_id: str, thread_id: str,
    keep_recent: int = KEEP_RECENT_MESSAGES,
) -> tuple[list[dict], list[dict]]:
    """(what may be summarised, what must be left alone).

    Read as the REQUESTER, not as the agent: findings §4.1 revoked
    comrade_agent's select on `messages` on purpose, and this is the same rule
    `recent_turns` follows. Compaction is not a reason to widen it — a summary
    is built from what that member can already read.

    The cursor is a KEYSET on `(created_at, id)` for the same reason capture's
    is: two messages can share a timestamp, and a cursor that skips a tied pair
    loses exactly the message somebody will ask about later.
    """
    state = working_state(team_id, thread_id)
    with user_session(requester_id) as conn:
        rows = conn.execute(
            _COMPACT_SQL,
            (team_id, thread_id, state["summary_through"],
             state["summary_through_id"]),
        ).fetchall()
    messages = [
        {"id": str(r[0]), "text": r[1], "sender": r[2], "created_at": r[3]}
        for r in rows
    ]
    if len(messages) <= keep_recent:
        return [], messages
    return messages[:-keep_recent], messages[-keep_recent:]


def working_state_content(team_id: str, thread_id: str):
    """The thread's established state as one model turn, or None if empty.

    Datamarked. A summary is written FROM member text, so it is data — and a
    summary that reaches the model unmarked is an injection surface with a very
    long memory: it is replayed on every subsequent turn of the thread.
    """
    from pipeline.parsers import spotlight

    state = working_state(team_id, thread_id)
    sections: list[str] = []
    if state["summary"]:
        sections.append(f"Earlier in this thread: {state['summary']}")
    if state["pins"]:
        pins = "; ".join(p.get("text", "") for p in state["pins"])
        sections.append(f"Constraints this thread has agreed: {pins}")
    if state["open_questions"]:
        questions = "; ".join(q.get("text", "") for q in state["open_questions"])
        sections.append(f"Still unanswered: {questions}")
    if state["workspace_revision"]:
        sections.append(f"Workspace revision: {state['workspace_revision']}")
    if not sections:
        return None
    return types.Content(
        role="user",
        parts=[types.Part(text=spotlight("\n".join(sections)))],
    )
