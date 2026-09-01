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
from pipeline.wiki import ORPHAN_TITLE, all_active_pages
from shared.consent import AGENT_PROPOSABLE, propose_action, propose_batch
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
                # The excerpt is VERBATIM source text — the most directly
                # attacker-shaped string this tool returns.
                "excerpt": spotlight(excerpt or ""),
            }
        )
    return {
        "title": page["title"],
        "description": page["description"],
        "facts": [
            {"fact": spotlight(f["text"]), "citations": by_entry.get(f["entry_id"], [])}
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
            "body": spotlight(body[:MESSAGE_BODY_CHARS]),
            "truncated": len(body) > MESSAGE_BODY_CHARS,
        }
        for message_id, body, thread_type, created_at, kind, name in rows
    ]


# Ranked full-text search over one team's ACTIVE wiki facts.
#
# `to_tsvector('english', v.fact)` is repeated verbatim from
# idx_memory_versions_fts, and `v.is_active` repeats its partial predicate; any
# other spelling and the planner cannot use the index. The same RLS caveat as
# _SEARCH_SQL applies — `@@` is not LEAKPROOF, so under `authenticated` the
# index is not reachable and idx_memory_versions_team bounds the scan instead.
#
# The joins are what make a hit usable rather than a bare string: the page
# title so the agent can open the rest of it, valid_from because §20.3.1
# measured a 39-point temporal gap on exactly this, and the first citation so
# the fact arrives with its provenance (§20.4's third advantage).
_MEMORY_SEARCH_SQL = (
    "select v.fact, v.valid_from, p.title,"
    "       (select c.source_kind from public.memory_citations c"
    "         where c.version_id = v.id order by c.created_at limit 1)"
    " from public.memory_versions v"
    " join public.memory_entries e on e.id = v.entry_id"
    " left join public.memory_pages p on p.id = e.page_id"
    " where v.team_id = %(team_id)s and v.is_active and not e.archived"
    " and to_tsvector('english', v.fact) @@ plainto_tsquery('english', %(q)s)"
    " order by ts_rank_cd(to_tsvector('english', v.fact),"
    " plainto_tsquery('english', %(q)s)) desc, v.valid_from desc"
    " limit %(limit)s"
)

MEMORY_SEARCH_LIMIT_DEFAULT = 8
MEMORY_SEARCH_LIMIT_MAX = 25


def search_memory(
    team_id: str,
    requester_id: str,
    query: str,
    limit: int = MEMORY_SEARCH_LIMIT_DEFAULT,
) -> list[dict]:
    """Ranked matches from this team's wiki, read as the requesting member.

    Beside the always-in-prompt page index, not instead of it (§20.4,
    corrected): the index is complete and page bodies load on demand, and the
    failure this covers is the narrow one — "the agent opened one page when the
    answer needed two."

    Two exclusions do the real work. `is_active` keeps SUPERSEDED facts out:
    memory_versions is bi-temporal, so a revised fact keeps its old row, and
    returning one would let the agent quote a value the team explicitly
    replaced — with a date and a citation attached, which reads as more
    authoritative than a guess rather than less. `not e.archived` keeps search
    agreeing with the page view, so a fact cannot be absent from the wiki and
    present here.

    Read as the member, so au_memory_versions_select is the gate. The explicit
    team_id is not redundant: `authenticated` has no current_team(), and a
    member of two teams would otherwise search both at once with no way to tell
    which wiki an answer came from.
    """
    q = query.strip()
    if not q:
        return []
    with user_session(requester_id) as conn:
        rows = conn.execute(
            _MEMORY_SEARCH_SQL,
            {
                "team_id": team_id,
                "q": q,
                "limit": max(1, min(limit, MEMORY_SEARCH_LIMIT_MAX)),
            },
        ).fetchall()
    return [
        {
            "fact": spotlight(fact),
            "valid_from": valid_from.isoformat() if valid_from else None,
            # Orphan facts have no page; naming the bucket keeps the shape
            # uniform so the agent never has to branch on null.
            "page": title or ORPHAN_TITLE,
            "source_kind": source_kind,
        }
        for fact, valid_from, title, source_kind in rows
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


def fetch_member_activity(team_id: str, requester_id: str) -> list[dict]:
    """Per-member recency: when each teammate was last visibly active.

    findings §3.1 lists this as missing and names the consequence — the `idle`
    nudge type exists, with templates, a cooldown and a suppression path all
    built, and NOTHING in the product could ever fire it because there was no
    recency signal to fire on.

    Governance (platform-findings memo, ruling 6) restricts person-to-person
    comparison. This answers "who might be stuck", not "who is doing least":
    it returns recency facts in membership order and never a ranking. Do not
    add a sort by volume — contribution_v already carries counts for the
    contribution screen, and mixing the two is how a coordination signal turns
    into a leaderboard.

    last_message_at is GROUP messages only. A private thread with the AI is
    not evidence of team participation, and counting it would mean a member
    who talks only to the bot never looks idle — the exact case the nudge
    exists for.
    """
    with user_session(requester_id) as conn:
        rows = conn.execute(
            "select c.user_id, p.display_name, c.last_message_at, c.last_task_at,"
            # Postgres greatest() IGNORES nulls and returns null only when
            # every argument is null — exactly the semantics wanted here. An
            # '-infinity' sentinel would work in SQL and then fail on the way
            # out: psycopg cannot load it into a datetime.
            #
            # The age is computed HERE rather than in Python so the comparison
            # uses one clock. Subtracting a Postgres timestamp from a Python
            # `datetime.now()` silently depends on the app host and the
            # database agreeing, which is the kind of assumption that holds
            # until it is 3am and a container's clock has drifted.
            " extract(day from now() - greatest(c.last_message_at,"
            "                                   c.last_task_at))::int"
            "   as days_since_last_signal"
            " from public.contribution_v c"
            " join public.profiles p on p.id = c.user_id"
            " where c.team_id = %s"
            " order by p.display_name",
            (team_id,),
        ).fetchall()
    return [
        {
            "user_id": str(user_id),
            "display_name": name,
            "last_message_at": last_msg.isoformat() if last_msg else None,
            "last_task_at": last_task.isoformat() if last_task else None,
            "days_since_last_signal": days,
        }
        for user_id, name, last_msg, last_task, days in rows
    ]


REPO_LIMIT_DEFAULT = 15
REPO_SUMMARY_CHARS = 600


def fetch_repo_activity(
    team_id: str,
    requester_id: str,
    limit: int = REPO_LIMIT_DEFAULT,
) -> list[dict]:
    """What has happened in this team's repositories, newest first.

    findings §14: connecting a repository is a core feature, not an
    integration. §16.1's comment on the schema says it outright — "AI queries
    this, not the raw repo". This reads the compiled event record, never
    GitHub's API, so it holds no token and cannot be made to fetch anything.

    Read as the requesting member (§4.1): the agent role has no grant on
    github_activity at all. The explicit team_id is the scoping — a member of
    two teams would otherwise see both repositories at once, and
    au_github_activity_select alone would allow it.
    """
    with user_session(requester_id) as conn:
        rows = conn.execute(
            "select a.node_type, a.author_github, a.occurred_at, a.payload,"
            " r.repo_full_name"
            " from public.github_activity a"
            " join public.github_repos r on r.id = a.repo_id"
            " where a.team_id = %s"
            " order by a.occurred_at desc nulls last, a.created_at desc"
            " limit %s",
            (team_id, max(1, min(limit, 50))),
        ).fetchall()

    out = []
    for node_type, author, occurred_at, payload, repo in rows:
        payload = payload or {}
        # Title first, then body: a reader wants the headline, and a PR body
        # can run to thousands of characters that would flood the turn.
        parts = [payload.get("title") or "", payload.get("body") or ""]
        full = "\n".join(p for p in parts if p).strip()
        out.append({
            "repo": repo,
            "node_type": node_type,
            "author": author,
            "occurred_at": occurred_at.isoformat() if occurred_at else None,
            "url": payload.get("url"),
            "summary": spotlight(full[:REPO_SUMMARY_CHARS]),
            "truncated": len(full) > REPO_SUMMARY_CHARS,
        })
    return out


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


def memory_search(query: str, tool_context: ToolContext) -> list[dict]:
    """Search the team wiki for facts matching some words.

    Your instructions already list every page by title and description — use
    that index and `memory_read_page` when you know which page an answer lives
    on. Use this instead when you do NOT: when the answer might be spread
    across several pages, when no title obviously covers it, or when a
    read_page came back without what you needed.

    Each hit gives the fact, the page it is on, the date it became true and
    where it came from. Quote the date — a fact from May and one from this
    morning are not equally reliable. Superseded facts are never returned, so
    what you get back is what the team currently believes. An empty list means
    the wiki does not record it; say that rather than guessing, and consider
    that the wording may differ from yours — this matches words, not meanings.

    Args:
        query: content words to look for, e.g. "deployment checklist" or
            "who owns the API". Not a question — "when is the demo" searches
            for the words "when", "demo".
    """
    return search_memory(
        tool_context.state["team_id"], tool_context.state["requester_id"], query
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


def team_propose_batch(items: list[dict], tool_context: ToolContext) -> dict:
    """Propose several related actions together as ONE reviewable group,
    instead of separate unrelated-looking cards. Use this when multiple
    actions belong to a single piece of work the member asked for — e.g.
    creating three tasks for one project kickoff — so the inbox shows them
    together with progress ("2 of 3 approved") instead of scattering them.

    This changes only how the proposals are DISPLAYED. Each one is still
    approved or rejected on its own — there is no batch-wide approve/reject,
    and approving some of them never approves or cancels the rest. Say
    you've proposed them, not that they're done.

    Args:
        items: one dict per proposal, each {"tool_name": "task_create" or
            "task_update", "args": {...}, "source": "..."}. `args` follows
            the same shape team_propose_task / task_propose_update build —
            task_create needs assignee_id/title/description/deadline,
            task_update needs task_id plus whichever fields are changing.
            `source` is the optional note shown on that item's card.
    """
    # This is the ONE place a tool name chosen by the model reaches the consent
    # queue. Every other proposal path names its tool as a literal. Until
    # member_depart existed, propose_action's `not in _EXECUTORS` check
    # happened to reject everything unexpected — accidental validation that
    # would have widened the moment the executor map grew. Say it out loud
    # instead, and refuse per item so one bad name does not discard four good
    # proposals (propose_batch's own partial-failure contract).
    allowed, refused = [], []
    for item in items:
        name = item.get("tool_name")
        if name in AGENT_PROPOSABLE:
            allowed.append(
                {
                    "tool_name": name,
                    "args": item.get("args") or {},
                    "source_snippet": item.get("source") or None,
                }
            )
        else:
            refused.append(
                {
                    "status": "failed",
                    "tool_name": name,
                    "error": f"{name!r} is not a tool the agent may propose",
                }
            )
    if not allowed:
        return {"batch_id": None, "items": refused}
    result = propose_batch(
        tool_context.state["team_id"],
        tool_context.state["requester_id"],
        allowed,
    )
    result["items"].extend(refused)
    return result


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


def member_activity(tool_context: ToolContext) -> list[dict]:
    """When each teammate was last visibly active: their last group message and
    last task movement, plus how many days ago that was.

    Use this to notice who might be stuck or out of the loop before nudging
    them. It is a coordination signal, not a performance measure — never rank
    members by it or compare them out loud.
    """
    return fetch_member_activity(
        tool_context.state["team_id"], tool_context.state["requester_id"]
    )


def repo_activity(tool_context: ToolContext) -> list[dict]:
    """Recent activity in this team's repositories: merges, reviews, issues and
    comments, newest first.

    Use this to answer what changed in the repo, who worked on what, and when.
    It reads the recorded event history, not the code — for what a change does,
    ask the person who made it rather than guessing from a title.
    """
    return fetch_repo_activity(
        tool_context.state["team_id"], tool_context.state["requester_id"]
    )
