"""Run a single agent turn and record it to agent_runs.

Conversation history is the messages table's job (source of truth); each turn
uses a fresh, stateless ADK runner seeded with the server-bound team/requester.
The orchestrator (run_turn / run_turn_sync) is added in Task 3.
"""
from typing import Any


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
