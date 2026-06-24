"""Run the agent on a prompt and capture the tool calls it made (live LLM)."""
import asyncio

from google.adk.runners import InMemoryRunner
from google.genai import types

from agent.agent import root_agent

_APP = "comrade-eval"


async def _run(prompt: str, state: dict) -> list[dict]:
    runner = InMemoryRunner(agent=root_agent, app_name=_APP)
    session = await runner.session_service.create_session(
        app_name=_APP, user_id="eval", state=state
    )
    msg = types.Content(role="user", parts=[types.Part(text=prompt)])
    calls: list[dict] = []
    async for ev in runner.run_async(
        user_id="eval", session_id=session.id, new_message=msg
    ):
        if not (ev.content and ev.content.parts):
            continue
        for part in ev.content.parts:
            fc = getattr(part, "function_call", None)
            if fc:
                calls.append({"name": fc.name, "args": dict(fc.args or {})})
    return calls


def run_agent(prompt: str, state: dict) -> list[dict]:
    return asyncio.run(_run(prompt, state))
