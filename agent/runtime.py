"""Run a single agent turn and record it to agent_runs.

Conversation history is the messages table's job (source of truth); each turn
uses a fresh, stateless ADK runner seeded with the server-bound team/requester.
The orchestrator (run_turn / run_turn_sync) is defined below the pure helpers.
"""
import asyncio
from typing import Any

from google.adk.runners import InMemoryRunner
from google.genai import types

from agent.agent import root_agent
from shared.agent_runs import append_step, finish_run, start_run

_APP_NAME = "comrade"


def _steps_from_event(event: Any, start_seq: int) -> list[dict[str, Any]]:
    """Map one ADK event's parts to ordered step dicts.

    Pure: reads function_call / function_response / text off each part. seq
    continues from start_seq so steps stay globally ordered across events.
    """
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) or []
    steps: list[dict[str, Any]] = []
    seq = start_seq
    for part in parts:
        fc = getattr(part, "function_call", None)
        fr = getattr(part, "function_response", None)
        text = getattr(part, "text", None)
        if fc is not None:
            steps.append({
                "seq": seq, "type": "tool_call",
                "tool": fc.name, "args": dict(fc.args or {}),
            })
            seq += 1
        elif fr is not None:
            steps.append({
                "seq": seq, "type": "tool_result",
                "tool": fr.name, "response": fr.response,
            })
            seq += 1
        elif text is not None:
            steps.append({"seq": seq, "type": "text", "text": text})
            seq += 1
    return steps


def _reply_from_steps(steps: list[dict[str, Any]]) -> str:
    """Concatenate the text steps into the user-facing reply."""
    return "".join(s["text"] for s in steps if s["type"] == "text").strip()


async def run_turn(
    team_id: str, requester_id: str, user_text: str, trigger_type: str = "user"
) -> dict[str, Any]:
    """Run one agent turn; record every step to agent_runs. Returns the result.

    team_id / requester_id are SERVER-BOUND here and injected into ADK session
    state — the model receives them via state, never as tool arguments.
    """
    run_id = start_run(team_id, trigger_type, user_text[:200])
    runner = InMemoryRunner(agent=root_agent, app_name=_APP_NAME)
    session = await runner.session_service.create_session(
        app_name=_APP_NAME, user_id=requester_id,
        state={"team_id": team_id, "requester_id": requester_id},
    )
    message = types.Content(role="user", parts=[types.Part(text=user_text)])
    all_steps: list[dict[str, Any]] = []
    try:
        async for event in runner.run_async(
            user_id=requester_id, session_id=session.id, new_message=message
        ):
            for step in _steps_from_event(event, len(all_steps)):
                append_step(team_id, run_id, step)
                all_steps.append(step)
    except Exception:
        finish_run(team_id, run_id, "failed")
        raise
    finish_run(team_id, run_id, "done")
    return {
        "run_id": run_id,
        "reply": _reply_from_steps(all_steps),
        "steps": all_steps,
    }


def run_turn_sync(
    team_id: str, requester_id: str, user_text: str, trigger_type: str = "user"
) -> dict[str, Any]:
    """Blocking wrapper around run_turn for sync callers (the HTTP handler).

    Must not be called from within a running event loop — asyncio.run() creates
    a new loop and raises if one is already running.
    """
    return asyncio.run(run_turn(team_id, requester_id, user_text, trigger_type))
