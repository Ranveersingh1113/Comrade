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

Scoping note: `authenticated` has no current_team(), and au_messages_select
legitimately lets a member read BOTH the group room and their own private
thread. So team_id / thread_type / thread_owner_id here are SCOPING filters
that RLS cannot do for us — the authorization is still RLS's job.
"""
from google.genai import types

from shared.db import user_session

# Newest-first with an explicit LIMIT (so the DB does the capping), reversed
# below into chronological order. `id is distinct from %s` also holds when the
# exclusion is NULL, so one statement covers both callers.
_SQL = (
    "select m.sender_kind, m.body, p.display_name"
    " from public.messages m"
    " left join public.profiles p on p.id = m.sender_id"
    " where m.team_id = %s and m.thread_type = %s"
    " and m.thread_owner_id is not distinct from %s::uuid"
    " and m.deleted_scope is null"
    " and m.id is distinct from %s::uuid"
    " order by m.created_at desc, m.id desc limit %s"
)


def recent_turns(
    team_id: str,
    requester_id: str,
    thread_type: str,
    limit: int,
    exclude_message_id: str | None = None,
) -> list[types.Content]:
    """The last `limit` undeleted messages of one thread, oldest first.

    exclude_message_id drops the message this turn is about: server/app.py
    persists the member's message BEFORE the runtime runs, so without it the
    model would receive the current question twice.

    Group bodies carry their sender's name — a room has several humans in it,
    and ADK's user role alone would merge them into one voice.
    """
    if limit <= 0:
        return []
    owner = requester_id if thread_type == "private" else None
    with user_session(requester_id) as conn:
        rows = conn.execute(
            _SQL, (team_id, thread_type, owner, exclude_message_id, limit)
        ).fetchall()
    return [
        types.Content(
            role="user" if kind == "user" else "model",
            parts=[types.Part(
                text=f"{name}: {body}"
                if thread_type == "group" and kind == "user" and name
                else body
            )],
        )
        for kind, body, name in reversed(rows)
    ]
