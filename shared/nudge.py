"""Private AI nudges — exempt from consent (the AI sends as itself).

Because there is no human gate, a cooldown prevents re-nudging the same member
about the same thing every analysis cycle. Bodies are templates, not LLM output.
"""
from shared.db import Role, team_session

_TEMPLATES = {
    "pending_task": "You've got an open task that could use a look when you get a chance.",
    "overdue_deadline": "A deadline you're on has slipped past — worth a quick update when you can.",
    "idle": "You've been quiet in the room for a bit. Nothing urgent — just checking in.",
    "unopened_doc": "A shared doc looks relevant to your part. Worth a look when you get a moment.",
}

# re-nudging the same (member, type, subject) within this window is suppressed
_COOLDOWN = "24 hours"


def send_nudge(
    team_id: str, member_id: str, nudge_type: str, subject: str | None = None
) -> dict:
    """Send a private nudge now, unless an identical one is within the cooldown."""
    body = _TEMPLATES.get(nudge_type)
    if body is None:
        raise ValueError(f"unknown nudge_type: {nudge_type}")

    with team_session(Role.AGENT, team_id) as conn:
        recent = conn.execute(
            "select 1 from public.nudge_log where team_id=%s and member_id=%s"
            " and nudge_type=%s and coalesce(subject,'')=coalesce(%s,'')"
            f" and created_at > now() - interval '{_COOLDOWN}'",
            (team_id, member_id, nudge_type, subject),
        ).fetchone()
        if recent is not None:
            return {"status": "suppressed", "reason": "cooldown"}

        conn.execute(
            "insert into public.messages (team_id, thread_type, thread_owner_id,"
            " sender_kind, body) values (%s,'private',%s,'ai',%s)",
            (team_id, member_id, body),
        )
        conn.execute(
            "insert into public.nudge_log (team_id, member_id, nudge_type, subject)"
            " values (%s,%s,%s,%s)",
            (team_id, member_id, nudge_type, subject),
        )
    return {"status": "sent"}
