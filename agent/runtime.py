"""Run a single agent turn and record it to agent_runs.

Conversation history is the messages table's job (source of truth); each turn
uses a fresh, stateless ADK runner seeded with the server-bound team/requester.
The orchestrator (run_turn / run_turn_sync) is defined below the pure helpers.
"""
import asyncio
from typing import Any, AsyncIterator

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from starlette.concurrency import run_in_threadpool

from agent.agent import APP_NAME, app
from shared.agent_runs import append_step, finish_run, start_run


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


async def stream_turn(
    team_id: str, requester_id: str, user_text: str, trigger_type: str = "user"
) -> AsyncIterator[dict[str, Any]]:
    """Run one turn, yielding each step as it happens and recording it.

    team_id / requester_id are SERVER-BOUND here and injected into ADK session
    state — the model receives them via state, never as tool arguments.

    Yields {"type": "run", ...} first, then one dict per step, then
    {"type": "final", ...}. run_turn() below consumes this, so the
    orchestration exists once rather than twice.
    """
    run_id = await run_in_threadpool(start_run, team_id, trigger_type, user_text[:200])
    yield {"type": "run", "run_id": run_id}

    # Runner(app=...), not InMemoryRunner: the App is what carries the
    # chokepoint plugin, and InMemoryRunner is ADK's dev-mode helper (§1).
    # The session service stays in-memory and per-turn — conversation history
    # is the messages table's job, not ADK's.
    runner = Runner(app=app, session_service=InMemorySessionService())
    session = await runner.session_service.create_session(
        app_name=APP_NAME, user_id=requester_id,
        state={"team_id": team_id, "requester_id": requester_id},
    )
    message = types.Content(role="user", parts=[types.Part(text=user_text)])
    all_steps: list[dict[str, Any]] = []
    try:
        async for event in runner.run_async(
            user_id=requester_id, session_id=session.id, new_message=message
        ):
            for step in _steps_from_event(event, len(all_steps)):
                await run_in_threadpool(append_step, team_id, run_id, step)
                all_steps.append(step)
                yield step
    except Exception:
        await run_in_threadpool(finish_run, team_id, run_id, "failed")
        raise
    await run_in_threadpool(finish_run, team_id, run_id, "done")
    yield {
        "type": "final",
        "run_id": run_id,
        "reply": _reply_from_steps(all_steps),
    }


async def run_turn(
    team_id: str, requester_id: str, user_text: str, trigger_type: str = "user"
) -> dict[str, Any]:
    """Batch form of stream_turn: drain it and return the collected result."""
    steps: list[dict[str, Any]] = []
    final: dict[str, Any] = {}
    async for item in stream_turn(team_id, requester_id, user_text, trigger_type):
        if item.get("type") == "run":
            continue
        if item.get("type") == "final":
            final = item
            continue
        steps.append(item)
    return {"run_id": final["run_id"], "reply": final["reply"], "steps": steps}


def run_turn_sync(
    team_id: str, requester_id: str, user_text: str, trigger_type: str = "user"
) -> dict[str, Any]:
    """Blocking wrapper around run_turn for sync callers (the HTTP handler).

    Must not be called from within a running event loop — asyncio.run() creates
    a new loop and raises if one is already running.
    """
    return asyncio.run(run_turn(team_id, requester_id, user_text, trigger_type))
