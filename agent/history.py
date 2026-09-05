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
from google.genai import types

from shared.db import Role, team_session, user_session

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
    " order by m.created_at desc, m.id desc limit %s"
)


def recent_turns(
    team_id: str,
    requester_id: str,
    thread_id: str,
    limit: int,
    exclude_message_id: str | None = None,
) -> list[types.Content]:
    """The last `limit` undeleted messages of one thread, oldest first.

    exclude_message_id drops the message this turn is about: server/app.py
    persists the member's message BEFORE the runtime runs, so without it the
    model would receive the current question twice.

    Shared-thread bodies carry their sender's name — ADK's user role alone
    would merge several people into one voice.
    """
    if limit <= 0:
        return []
    with user_session(requester_id) as conn:
        rows = conn.execute(
            _SQL, (team_id, thread_id, exclude_message_id, limit)
        ).fetchall()
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
) -> list[tuple[str, str]]:
    """Participant messages added after a run started and not yet shown to it."""
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
            "select m.id, m.body from public.messages m"
            " where m.team_id=%s and m.thread_id=%s and m.sender_kind='user'"
            " and m.id is distinct from %s::uuid"
            " and m.created_at >= %s"
            " and not (m.id = any(%s::uuid[]))"
            " order by m.created_at, m.id",
            (team_id, thread_id, input_message_id, started_at, seen),
        ).fetchall()
    return [(str(message_id), body) for message_id, body in rows]
