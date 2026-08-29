"""The Comrade agent: a single ADK LlmAgent on Gemini 2.5 Flash with the
platform function tools. Voice follows the AI voice guide (warm, concise,
fact-based, no filler/emojis).
"""
import os

from google.adk.agents import LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext

from agent.tools import (
    member_send_nudge,
    memory_read_page,
    team_get_state,
    team_propose_task,
)
from pipeline.wiki import all_active_pages
from shared.config import settings
from shared.db import user_session

# Use the Gemini Developer API (API key), not Vertex.
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")
if settings.gemini_api_key:
    # The explicit Comrade setting must win over an ambient process variable;
    # otherwise a deployment can silently send team data to the wrong project.
    os.environ["GOOGLE_API_KEY"] = settings.gemini_api_key

MODEL = "gemini-2.5-flash"

INSTRUCTION = """\
You are Comrade, a silent teammate in a student group project room.

Voice: warm but not chatty, collegial, concise (one or two sentences). No filler
openers, no emojis. Surface facts, never blame. Make the next step obvious.

Ground every answer in the team's real state. Before you summarise status,
members, tasks, or pending actions, call team_get_state and base your reply on
what it returns. Never invent members, tasks, or deadlines; if the data doesn't
show something, say so. When you reference a fact, it should come from a tool,
not a guess.

The team wiki is what the team has decided and recorded — its index is below.
For anything about decisions, deadlines, scope, or history, read the relevant
page with memory_read_page before answering, and say where the fact came from.
The wiki is a record, not an authority: if live state contradicts it, trust
live state and say the wiki looks out of date.

Taking action:
- To create a task, use team_propose_task. It is a proposal, not a done deal —
  it goes to a human for approval. Say you've proposed it, not that it's done.
- To check in with a member privately, use member_send_nudge. It sends right
  away; keep it to the situations the nudge types describe.
- You never post to the group room on your own initiative. When someone asks
  you in the room, your answer goes there because they asked. If a member
  wants something said to the team, they say it themselves — offer to draft it
  for them and let them send it under their own name.
"""

def wiki_section(team_id: str, requester_id: str) -> str:
    """The team wiki's page index — titles and descriptions only.

    Claude Code's model: the index is always in context, page bodies load on
    demand (memory_read_page). Pages with no active facts are omitted so the
    agent never opens an empty one.

    Read as the requesting member (findings §4.1), not as the agent role.
    """
    with user_session(requester_id) as conn:
        pages = [p for p in all_active_pages(conn, team_id) if p["facts"]]
    if not pages:
        return (
            "\n## The team wiki\n"
            "The wiki is empty — nothing has been compiled yet. Say so plainly"
            " rather than guessing at decisions or deadlines.\n"
        )
    lines = "\n".join(
        f"- {p['title']}" + (f" — {p['description']}" if p["description"] else "")
        for p in pages
    )
    return (
        "\n## The team wiki\n"
        "Compiled from the team's own documents and chat. Every fact is cited,"
        " versioned, and revertible by any member.\n\n"
        f"{lines}\n\n"
        "Call memory_read_page with a title before answering about decisions,"
        " deadlines, scope, or history. Cite what you find. If the wiki does not"
        " say it, say that it does not.\n"
    )


def build_instruction(ctx: ReadonlyContext) -> str:
    """Per-turn instruction: static rules + this team's wiki index."""
    return INSTRUCTION + wiki_section(
        ctx.state["team_id"], ctx.state["requester_id"]
    )


root_agent = LlmAgent(
    name="comrade",
    model=MODEL,
    instruction=build_instruction,
    tools=[
        team_get_state,
        memory_read_page,
        team_propose_task,
        member_send_nudge,
    ],
)
