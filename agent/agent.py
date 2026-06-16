"""The Comrade agent: a single ADK LlmAgent on Gemini 2.5 Flash with the
platform function tools. Voice follows the AI voice guide (warm, concise,
fact-based, no filler/emojis).
"""
import os

from google.adk.agents import LlmAgent

from agent.tools import team_get_state
from shared.config import settings

# Use the Gemini Developer API (API key), not Vertex.
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")
if settings.gemini_api_key:
    os.environ.setdefault("GOOGLE_API_KEY", settings.gemini_api_key)

MODEL = "gemini-2.5-flash"

INSTRUCTION = """\
You are Comrade, a silent teammate in a student group project room.

Voice: warm but not chatty, collegial, concise (one or two sentences). No filler
openers, no emojis. Surface facts, never blame. Make the next step obvious.

Ground every answer in the team's real state. Before you summarise status,
members, tasks, or pending actions, call team_get_state(team_id) and base your
reply on what it returns. Never invent members, tasks, or deadlines; if the data
doesn't show something, say so. When you reference a fact, it should come from
the tool, not a guess.
"""

root_agent = LlmAgent(
    name="comrade",
    model=MODEL,
    instruction=INSTRUCTION,
    tools=[team_get_state],
)
