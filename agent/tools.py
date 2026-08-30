"""Platform tools for the ADK agent.

Pure functions (fetch_team_state) hold the logic and are unit-tested directly.
The ADK-facing tools are thin wrappers that bind team_id / requester_id from
the session state (server-set), never from LLM arguments — so the model cannot
read another team or attribute an action to someone else.
"""
import uuid
from datetime import datetime, timezone
from typing import Literal

from google.adk.tools import ToolContext

from pipeline.parsers import spotlight
from pipeline.wiki import all_active_pages
from shared.consent import propose_action
from shared.db import user_session
from shared.nudge import send_nudge

# Payload caps. Every result of these two tools is pasted into the next LLM
# call, so an uncapped read is an unbounded bill on every turn that makes one.
# Chat messages are short — 400 chars returns the great majority whole, and a
# full page of 8 costs ~800 tokens. 6000 chars of document is a couple of
# pages: enough to answer from, far short of a 200-page PDF's parsed_text.
# Both truncations set a `truncated` flag so the model can say it saw only part.
SEARCH_LIMIT_DEFAULT = 8
SEARCH_LIMIT_MAX = 20
MESSAGE_BODY_CHARS = 400
DOC_CHARS = 6000


def fetch_team_state(team_id: str, requester_id: str) -> dict:
    """Snapshot a team's coordination state, read as the requesting member.

    findings §4.1: the agent borrows the requester's permissions rather than
    holding its own. Every query carries an explicit team_id because
    `authenticated` can see every team the member belongs to — there is no
    current_team() to scope by, unlike the worker roles.

    Consequence, and correct: open_consent returns only the caller's OWN
    pending items, because that is what au_consent_queue_select allows.
    """
    with user_session(requester_id) as conn:
        team = conn.execute(
            "select id, name from public.teams where id = %s", (team_id,)
        ).fetchone()
        if team is None:
            return {"error": "team not found or not accessible"}

        members = conn.execute(
            "select m.user_id, p.display_name, m.role"
            " from public.memberships m"
            " join public.profiles p on p.id = m.user_id"
            " where m.team_id = %s and m.status = 'active'"
            " order by m.role, p.display_name",
            (team_id,),
        ).fetchall()

        tasks = conn.execute(
            "select id, title, assignee_id, status, deadline"
            " from public.tasks where team_id = %s and status <> 'done'"
            " order by deadline nulls last",
            (team_id,),
        ).fetchall()

        consent = conn.execute(
            "select id, tool_name, requesting_member_id"
            " from public.consent_queue"
            " where team_id = %s and status = 'pending'"
            " order by created_at",
            (team_id,),
        ).fetchall()

    return {
        "team": {"id": str(team[0]), "name": team[1]},
        "members": [
            {"user_id": str(r[0]), "display_name": r[1], "role": r[2]}
            for r in members
        ],
        "tasks": [
            {
                "id": str(r[0]),
                "title": r[1],
                "assignee_id": str(r[2]) if r[2] else None,
                "status": r[3],
                "deadline": r[4].isoformat() if r[4] else None,
            }
            for r in tasks
        ],
        "open_consent": [
            {
                "id": str(r[0]),
                "tool_name": r[1],
                "requesting_member_id": str(r[2]) if r[2] else None,
            }
            for r in consent
        ],
    }


def read_memory_page(team_id: str, requester_id: str, title: str) -> dict:
    """One wiki page's active facts with their citations, read as the member.

    Titles match case-insensitively — the model reads them off an index, so a
    capitalisation slip should not read as "no such page".
    """
    with user_session(requester_id) as conn:
        pages = [p for p in all_active_pages(conn, team_id) if p["facts"]]
        page = next(
            (p for p in pages if p["title"].lower() == title.strip().lower()), None
        )
        if page is None:
            return {
                "error": "no such page",
                "available": [p["title"] for p in pages],
            }
        entry_ids = [f["entry_id"] for f in page["facts"]]
        rows = conn.execute(
            "select v.entry_id, c.source_kind, c.source_id, c.excerpt"
            " from public.memory_versions v"
            " join public.memory_citations c on c.version_id = v.id"
            " where v.entry_id = any(%s::uuid[]) and v.is_active",
            (entry_ids,),
        ).fetchall()

    by_entry: dict[str, list[dict]] = {}
    for entry_id, source_kind, source_id, excerpt in rows:
        by_entry.setdefault(str(entry_id), []).append(
            {
                "source_kind": source_kind,
                "source_id": str(source_id),
                "excerpt": excerpt,
            }
        )
    return {
        "title": page["title"],
        "description": page["description"],
        "facts": [
            {"fact": f["text"], "citations": by_entry.get(f["entry_id"], [])}
            for f in page["facts"]
        ],
    }


# Ranked full-text search over one team's messages, read as the member.
#
# `to_tsvector('english', m.body)` is repeated VERBATIM from the definition of
# idx_messages_fts (a GIN over the expression, not a stored column); any other
# spelling and the planner cannot match the index at all. See the report for
# the measured caveat: under `authenticated` the RLS quals sit below this one,
# and because ts_match_vq is not LEAKPROOF PostgreSQL refuses to promote `@@`
# into an index condition, so the FTS index is reachable only without RLS. The
# leakproof `team_id = %s` IS promotable, and idx_messages_team_thread is what
# bounds the scan to one team in practice.
#
# ponytail: one team's messages scanned per search. Fine at pilot scale
# (~100ms over 24k rows measured). If a room outgrows it, the fix is a
# LEAKPROOF `@@` (superuser, unavailable on Supabase) or narrowing by date.
_SEARCH_SQL = (
    "select m.id, m.body, m.thread_type, m.created_at, m.sender_kind,"
    " p.display_name"
    " from public.messages m"
    " left join public.profiles p on p.id = m.sender_id"
    " where m.team_id = %(team_id)s and m.deleted_scope is null"
    " and to_tsvector('english', m.body) @@ plainto_tsquery('english', %(q)s)"
    " order by ts_rank_cd(to_tsvector('english', m.body),"
    " plainto_tsquery('english', %(q)s)) desc, m.created_at desc"
    " limit %(limit)s"
)


def search_messages(
    team_id: str,
    requester_id: str,
    query: str,
    limit: int = SEARCH_LIMIT_DEFAULT,
) -> list[dict]:
    """Ranked matches from this team's chat, read as the requesting member.

    findings §2.1/§4.1: comrade_agent has no SELECT on `messages` at all, so
    this runs under the member's own RLS and au_messages_select — is_team_member
    AND (group OR thread_owner_id = auth.uid()) — is what keeps another
    member's private thread out of the results. The explicit team_id is not
    optional: `authenticated` has no current_team(), and a member of two teams
    would otherwise search both at once.

    Tombstoned messages (deleted_scope set) are excluded — a message someone
    retracted must not come back through search.
    """
    with user_session(requester_id) as conn:
        rows = conn.execute(
            _SEARCH_SQL,
            {
                "team_id": team_id,
                "q": query.strip(),
                "limit": max(1, min(limit, SEARCH_LIMIT_MAX)),
            },
        ).fetchall()
    return [
        {
            "message_id": str(message_id),
            "sender": "Comrade" if kind == "ai" else (name or "a former member"),
            "thread": thread_type,
            "created_at": created_at.isoformat(),
            "body": body[:MESSAGE_BODY_CHARS],
            "truncated": len(body) > MESSAGE_BODY_CHARS,
        }
        for message_id, body, thread_type, created_at, kind, name in rows
    ]


def read_document(team_id: str, requester_id: str, document_id: str) -> dict:
    """One document's extracted text, read as the requesting member.

    au_documents_select gates it (any member of the team may read a team
    document); the explicit team_id keeps a member of two teams from reaching
    the other team's upload by id. Soft-deleted documents are gone for good.

    The text is SPOTLIGHTED on the way out. parsed_text is stored unmarked
    because it is source text, not a prompt — but it is attacker-controlled
    (anyone may upload a PDF that says "ignore your instructions"), so the
    datamarking the compile path applies before its LLM call has to be applied
    here too. The agent's instruction declares the marker, as _EXTRACT_SYSTEM
    does for the compiler.
    """
    try:
        document_id = str(uuid.UUID(str(document_id)))
    except ValueError:
        # The model can invent an id; that is a miss, not a crashed turn.
        return {"error": "no such document"}
    with user_session(requester_id) as conn:
        row = conn.execute(
            "select filename, kind, status, created_at, parsed_text"
            " from public.documents"
            " where id = %s and team_id = %s and deleted_at is null",
            (document_id, team_id),
        ).fetchone()
    if row is None:
        return {"error": "no such document"}
    filename, kind, status, created_at, text = row
    text = text or ""
    return {
        "document_id": document_id,
        "filename": filename,
        "kind": kind,
        "status": status,
        "created_at": created_at.isoformat(),
        "truncated": len(text) > DOC_CHARS,
        "text": spotlight(text[:DOC_CHARS]),
    }


def fetch_task(team_id: str, requester_id: str, task_id: str) -> dict:
    """One task's full detail, read as the requesting member.

    Same reasoning as fetch_team_state: `authenticated` has no current_team(),
    so the explicit team_id keeps a member of two teams from reaching the
    other team's task by id (tests/test_task_tools.py proves this
    non-vacuously: the member can genuinely read the other team's task once
    scoped to it, and genuinely cannot once misscoped).
    """
    try:
        task_id = str(uuid.UUID(str(task_id)))
    except ValueError:
        return {"error": "no such task"}
    with user_session(requester_id) as conn:
        row = conn.execute(
            "select t.title, t.description, t.status, p.display_name,"
            " t.deadline, t.confirmed_at, t.created_at"
            " from public.tasks t"
            " left join public.profiles p on p.id = t.assignee_id"
            " where t.id = %s and t.team_id = %s",
            (task_id, team_id),
        ).fetchone()
    if row is None:
        return {"error": "no such task"}
    title, description, status, assignee, deadline, confirmed_at, created_at = row
    return {
        "task_id": task_id,
        "title": title,
        "description": description,
        "status": status,
        "assignee": assignee,
        "deadline": deadline.isoformat() if deadline else None,
        "confirmed_at": confirmed_at.isoformat() if confirmed_at else None,
        "created_at": created_at.isoformat(),
    }


def propose_task_update(
    team_id: str,
    requester_id: str,
    task_id: str,
    title: str = "",
    description: str | None = None,
    deadline: str | None = None,
    assignee_id: str = "",
    source: str | None = None,
) -> dict:
    """Propose amending an existing task. Pure, unit-testable half of
    task_propose_update.

    Only title/description/deadline/assignee_id may ever change — never
    status or confirmed_at (trg_tasks_confirm_guard: only the assignee may
    confirm their own task, and the executor's auth.uid() is NULL, so that
    must stay a human-only transition; shared/consent.py's _exec_task_update
    refuses a proposal that reaches for either). Reassignment is allowed —
    the trigger itself voids any prior confirmation when assignee_id changes.

    "Not mentioned" vs "explicitly cleared": title/assignee_id follow
    team_propose_task's own convention — "" means not mentioned, because a
    task cannot be retitled to blank or unassigned through this tool.
    description/deadline are genuinely optional to CHANGE, so they get a
    third state: the Python default None (the caller omitted the argument)
    leaves the column alone; "" clears it to null; anything else is the new
    value.
    """
    args: dict = {"task_id": task_id}
    if title:
        args["title"] = title
    if assignee_id:
        args["assignee_id"] = assignee_id
    if description is not None:
        args["description"] = description or None
    if deadline is not None:
        args["deadline"] = deadline or None
    return propose_action(
        team_id=team_id,
        requester_id=requester_id,
        tool_name="task_update",
        args=args,
        source_snippet=source or None,
        reversible=True,
    )


# ---------------------------------------------------------------------------
# ADK tools — team_id / requester_id are server-bound from session state.
# ---------------------------------------------------------------------------

def team_get_state(tool_context: ToolContext) -> dict:
    """Get the current team's state: members, live tasks, and pending consent
    items. Call this before summarising status or referencing who/what exists."""
    return fetch_team_state(
        tool_context.state["team_id"], tool_context.state["requester_id"]
    )


def now(tool_context: ToolContext) -> dict:
    """The current UTC time, ISO-8601. Takes no arguments.

    Call this before judging any deadline as overdue, due soon, or already
    passed — never assume today's date from context or from the conversation.
    """
    return {"now": datetime.now(timezone.utc).isoformat()}


def task_get(task_id: str, tool_context: ToolContext) -> dict:
    """Read one task's full detail: title, description, status, assignee,
    deadline, confirmation time, and when it was created.

    Use this to check a specific task rather than relying on team_get_state's
    short summary. Call now() first if you need to judge whether its deadline
    has passed. Returns an error if the task does not belong to this team.

    Args:
        task_id: the task's id, e.g. from team_get_state's task list.
    """
    return fetch_task(
        tool_context.state["team_id"], tool_context.state["requester_id"], task_id
    )


def memory_read_page(title: str, tool_context: ToolContext) -> dict:
    """Read one page of the team wiki: its facts and where each came from.

    Use this before answering about decisions, deadlines, scope, or history.
    The page titles are listed in your instructions. Cite what you find. If a
    page does not contain the answer, say the wiki does not record it.

    Args:
        title: a page title from the wiki index in your instructions.
    """
    return read_memory_page(
        tool_context.state["team_id"], tool_context.state["requester_id"], title
    )


def messages_search(query: str, tool_context: ToolContext) -> list[dict]:
    """Search what has actually been said in this team's chat.

    Use this whenever the question is about something said, agreed, asked or
    promised in conversation — search for it rather than guessing. It covers
    the group room and your private thread with the person asking; you cannot
    see anyone else's private thread, so say so rather than speculating.
    Each result gives the sender, the thread, when it was sent, and the text —
    quote who said it and when. `truncated` means the body was cut short.
    An empty list means nothing matched; say that instead of inventing a quote.

    Args:
        query: the words to look for, e.g. "demo deadline" or "who owns the
            slides". Content words only — it matches on words, not phrases.
    """
    return search_messages(
        tool_context.state["team_id"], tool_context.state["requester_id"], query
    )


def document_read(document_id: str, tool_context: ToolContext) -> dict:
    """Open one of the team's uploaded documents and read its text.

    Use this when a wiki citation points at a document (source_kind
    "document") and you need more than the excerpt, or when someone asks what
    a document says. Returns filename, kind, when it was uploaded, and the
    text; `truncated` true means you were given only the start of it, so say
    so rather than implying you read the whole thing. Returns an error if the
    document does not belong to this team or has been deleted.

    Args:
        document_id: the document's id, e.g. from a wiki citation's source_id.
    """
    return read_document(
        tool_context.state["team_id"], tool_context.state["requester_id"], document_id
    )


def team_propose_task(
    assignee_id: str,
    title: str,
    description: str,
    deadline: str,
    source: str,
    tool_context: ToolContext,
) -> dict:
    """Propose creating a task. This is GATED: it is NOT created now — it goes to
    the requester for approval and is performed only if they approve.

    Args:
        assignee_id: user id the task is for (must be an active team member).
        title: short task title.
        description: optional detail ("" if none).
        deadline: optional ISO datetime ("" if none).
        source: short note on what prompted this (shown on the consent card).
    """
    args = {
        "assignee_id": assignee_id or None,
        "title": title,
        "description": description or None,
        "deadline": deadline or None,
    }
    return propose_action(
        team_id=tool_context.state["team_id"],
        requester_id=tool_context.state["requester_id"],
        tool_name="task_create",
        args=args,
        source_snippet=source or None,
        reversible=True,
    )


def task_propose_update(
    task_id: str,
    tool_context: ToolContext,
    title: str = "",
    description: str | None = None,
    deadline: str | None = None,
    assignee_id: str = "",
    source: str = "",
) -> dict:
    """Propose amending an existing task: retitle it, redescribe it,
    reschedule its deadline, or reassign it. This is GATED: nothing changes
    now — it goes to the requester for approval and only happens if they
    approve. Say you've proposed it, not that it's done.

    You can never change a task's status or confirm it on someone's behalf —
    only the assignee can do that themselves.

    Leave a parameter out to leave that field alone. For description and
    deadline, pass "" to clear it rather than change it. title and
    assignee_id cannot be cleared this way — pass "" to leave them alone too.

    Args:
        task_id: the task to amend (from team_get_state or task_get).
        title: new title, or "" to leave it alone.
        description: new description, "" to clear it, or omit to leave it alone.
        deadline: new ISO datetime, "" to clear it, or omit to leave it alone.
        assignee_id: new assignee's user id (must be an active member), or "".
        source: short note on what prompted this (shown on the consent card).
    """
    return propose_task_update(
        tool_context.state["team_id"],
        tool_context.state["requester_id"],
        task_id,
        title=title,
        description=description,
        deadline=deadline,
        assignee_id=assignee_id,
        source=source or None,
    )


def member_send_nudge(
    member_id: str,
    nudge_type: Literal["pending_task", "overdue_deadline", "idle", "unopened_doc"],
    subject: str,
    tool_context: ToolContext,
) -> dict:
    """Send a private nudge to a member's own thread. Acts immediately (no
    consent — the AI sends as itself). A 24h cooldown per (member, type, subject)
    prevents repeats.

    Args:
        member_id: the member to nudge.
        nudge_type: which gentle template to use.
        subject: what it's about (e.g. a task id), for cooldown dedupe ("" if n/a).
    """
    return send_nudge(
        tool_context.state["team_id"], member_id, nudge_type, subject or None
    )
