"""Platform tools for the ADK agent.

These are plain Python functions (no MCP server). Each runs under the
team-scoped `comrade_agent` role via shared.db.team_session, so RLS confines
every query to the given team. They follow the "state-with-result" pattern:
return enough context in one call to save the agent discovery turns.
"""
from shared.db import Role, team_session


def team_get_state(team_id: str) -> dict:
    """Snapshot a team's current coordination state in a single call.

    Returns the team, its active members (with roles), all not-done tasks, and
    any pending consent items — so the agent can reason without extra lookups.

    Args:
        team_id: the team (room) to inspect.
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
