"""Platform tools for the ADK agent.

Pure functions (fetch_team_state) hold the logic and are unit-tested directly.
The ADK-facing tools are thin wrappers that bind team_id / requester_id from
the session state (server-set), never from LLM arguments — so the model cannot
read another team or attribute an action to someone else.
"""
from typing import Literal

from google.adk.tools import ToolContext

from pipeline.wiki import all_active_pages
from shared.consent import propose_action
from shared.db import Role, team_session
from shared.nudge import send_nudge


def fetch_team_state(team_id: str) -> dict:
    """Snapshot a team's current coordination state (pure; explicit team_id).

    Returns the team, active members (with roles), all not-done tasks, and any
    pending consent items.
    """
    with team_session(Role.AGENT, team_id) as conn:
        team = conn.execute("select id, name from public.teams").fetchone()
        if team is None:
            return {"error": "team not found or not accessible"}

        members = conn.execute(
            "select m.user_id, p.display_name, m.role"
            " from public.memberships m"
            " join public.profiles p on p.id = m.user_id"
            " where m.status = 'active'"
            " order by m.role, p.display_name"
        ).fetchall()

        tasks = conn.execute(
            "select id, title, assignee_id, status, deadline"
            " from public.tasks where status <> 'done'"
            " order by deadline nulls last"
        ).fetchall()

        consent = conn.execute(
            "select id, tool_name, requesting_member_id"
            " from public.consent_queue where status = 'pending'"
            " order by created_at"
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


def read_memory_page(team_id: str, title: str) -> dict:
    """One wiki page's active facts with their citations (pure; explicit team).

    Titles match case-insensitively — the model reads them off an index, so a
    capitalisation slip should not read as "no such page".
    """
    with team_session(Role.AGENT, team_id) as conn:
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


# ---------------------------------------------------------------------------
# ADK tools — team_id / requester_id are server-bound from session state.
# ---------------------------------------------------------------------------

def team_get_state(tool_context: ToolContext) -> dict:
    """Get the current team's state: members, live tasks, and pending consent
    items. Call this before summarising status or referencing who/what exists."""
    return fetch_team_state(tool_context.state["team_id"])


def memory_read_page(title: str, tool_context: ToolContext) -> dict:
    """Read one page of the team wiki: its facts and where each came from.

    Use this before answering about decisions, deadlines, scope, or history.
    The page titles are listed in your instructions. Cite what you find. If a
    page does not contain the answer, say the wiki does not record it.

    Args:
        title: a page title from the wiki index in your instructions.
    """
    return read_memory_page(tool_context.state["team_id"], title)


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
