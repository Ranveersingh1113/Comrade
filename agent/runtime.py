"""Run a single agent turn and record it to agent_runs.

Conversation history is the messages table's job (source of truth); each turn
uses a fresh ADK runner whose in-memory session is REPLAYED from that table
(agent/history.py) before the new message lands, so the agent remembers the
thread it is standing in without a second session store.
The orchestrator (run_turn / run_turn_sync) is defined below the pure helpers.
"""
import asyncio
import logging
from contextlib import ExitStack
from typing import Any, AsyncIterator

from google.adk.agents.run_config import RunConfig
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from starlette.concurrency import run_in_threadpool

from agent.agent import APP_NAME, app
from agent.history import recent_turns
from agent.repo_tools import connected_repo
from shared.agent_runs import append_step, finish_run, start_run
from shared.db import room_lock
from shared.config import settings

logger = logging.getLogger(__name__)


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


def _finish(
    team_id: str, run_id: str, status: str, used_input: int, used_output: int
) -> None:
    """finish_run with positional arguments — run_in_threadpool forwards no
    keywords, and the usage parameters are keyword-only so a caller cannot
    silently swap the two token counts."""
    finish_run(
        team_id, run_id, status,
        input_tokens=used_input, output_tokens=used_output,
    )


def _usage_from_event(event: Any) -> tuple[int, int]:
    """(prompt tokens, generated tokens) for one model call, or (0, 0).

    Defensive about the shape because it comes from a vendor SDK and is not
    load-bearing: a turn that produced work must never fail because the
    accounting could not read a field. Absent usage is recorded as zero, which
    understates rather than invents.

    Accumulating these ACROSS the empty-turn retries is deliberate. A retried
    turn genuinely paid for its prompt each time — the measured 1-in-8 empty
    response costs the whole context again, and that has been invisible since
    the retry shipped.
    """
    usage = getattr(event, "usage_metadata", None)
    if usage is None:
        return 0, 0
    prompt = getattr(usage, "prompt_token_count", None) or 0
    # `candidates_token_count` is what Gemini calls generated tokens. Fall back
    # to total - prompt, which is what the empty-turn instrumentation showed
    # arriving when the candidate list was empty.
    output = getattr(usage, "candidates_token_count", None)
    if output is None:
        total = getattr(usage, "total_token_count", None) or 0
        output = max(total - prompt, 0)
    return int(prompt), int(output)


def _reply_from_steps(steps: list[dict[str, Any]]) -> str:
    """Concatenate the text steps into the user-facing reply."""
    return "".join(s["text"] for s in steps if s["type"] == "text").strip()


# How many times to re-ask when the model returns literally nothing.
#
# 🔴 ROOT CAUSE, measured 2026-09-01. The empty turn is not an ADK bug and not
# ours: Gemini intermittently returns a candidate with an EMPTY parts list and
# a perfectly normal finish_reason of STOP. Instrumented, one looks like this —
#
#     finish_reason  STOP          error_code  None      n_parts  0
#     content        not None      prompt_token_count  3180
#                                  total_token_count   3180   <- 0 output
#
# No safety block, no truncation, no error, no exception: it was handed 3180
# tokens of prompt and generated none at all. Nothing downstream can
# distinguish that from a model with nothing to say, which is why it reached
# the member as silence.
#
# Retrying is safe HERE and would not be anywhere else, and that is the whole
# argument for this fix: an empty turn is by definition a turn with no side
# effects — no tool ran, no consent row was written, nothing was yielded to the
# caller. The `if all_steps` guard is what keeps that true. A turn that called
# a tool and THEN went quiet must never be retried; it would run the tool twice.
#
# Three attempts, not more: measured 1-in-8 empty, so a third failure is ~1 in
# 500 and is more likely to be something systematic than bad luck — at which
# point the honest `empty` frame below is the right answer rather than a fourth
# call on the member's budget.
EMPTY_TURN_ATTEMPTS = 3


async def stream_turn(
    team_id: str,
    requester_id: str,
    user_text: str,
    trigger_type: str = "user",
    thread_type: str = "private",
    exclude_message_id: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run one turn, yielding each step as it happens and recording it.

    team_id / requester_id are SERVER-BOUND here and injected into ADK session
    state — the model receives them via state, never as tool arguments.

    thread_type says WHICH conversation this is; history is thread-scoped, and
    a private turn must never be shown the room (or vice versa).
    exclude_message_id is the member's just-persisted message: the server
    writes it before calling us, so without this the model gets it twice.

    Yields {"type": "run", ...} first, then one dict per step, then
    {"type": "final", ...}. run_turn() below consumes this, so the
    orchestration exists once rather than twice.
    """
    # findings §4.3: one agent turn at a time per ROOM. Two runs in one group
    # room are two agents that cannot see each other — duplicated work,
    # contradictory answers, and a race on the consent queue. Private threads
    # share no surface, so they stay parallel and take no lock at all.
    #
    # ExitStack because the lock's lifetime is the whole turn but only SOME
    # turns take one; a plain `with` would need the body duplicated.
    with ExitStack() as stack:
        if thread_type == "group":
            if not stack.enter_context(room_lock(team_id)):
                # Decision Q6: say what is actually happening. A spinner here
                # reads as a hang, and the member has no way to tell the
                # difference between "thinking" and "stuck".
                yield {
                    "type": "busy",
                    "detail": (
                        "Comrade is working on someone else's question in this"
                        " room. Yours is next — send it again in a moment."
                    ),
                }
                return

        run_id = await run_in_threadpool(
            start_run, team_id, trigger_type, user_text[:200]
        )
        yield {"type": "run", "run_id": run_id}

        message = types.Content(role="user", parts=[types.Part(text=user_text)])
        all_steps: list[dict[str, Any]] = []
        # Outside the retry loop on purpose: a retried empty turn paid
        # for its prompt every attempt, and that is the number worth
        # having.
        used_input = 0
        used_output = 0
        # Inside the try: a failed history read must close the run row too, not
        # leave it 'running' forever.
        try:
            history = await run_in_threadpool(
                recent_turns, team_id, requester_id, thread_type,
                settings.agent_history_turns, exclude_message_id,
            )
            # Resolved once per turn rather than per tool call: it is a DB read
            # and it cannot change mid-turn.
            repo = await run_in_threadpool(connected_repo, team_id, requester_id)
            for attempt in range(1, EMPTY_TURN_ATTEMPTS + 1):
                # Runner(app=...), not InMemoryRunner: the App is what carries
                # the chokepoint plugin, and InMemoryRunner is ADK's dev-mode
                # helper (§1). The session service stays in-memory and
                # per-turn; it is FILLED from `messages` rather than replaced
                # by ADK's DatabaseSessionService, whose unqualified tables
                # would sit in `public` with no RLS (agent/history.py).
                #
                # Rebuilt per attempt: ADK appends what it produced to the
                # session, so retrying on the same one would resend the empty
                # candidate as context.
                runner = Runner(app=app, session_service=InMemorySessionService())
                session = await runner.session_service.create_session(
                    app_name=APP_NAME, user_id=requester_id,
                    state={
                        "team_id": team_id,
                        "requester_id": requester_id,
                        # Server-bound like the two above. The repo tools
                        # derive the checkout path from these; the model names
                        # neither, so it cannot ask to work in another team's
                        # tree or another team's repository.
                        "repo_full_name": repo,
                    },
                )
                for content in history:
                    # A model-role event must be authored by this agent, or ADK
                    # relabels it as another agent's reply and rewrites it into
                    # a "For context:" note.
                    await runner.session_service.append_event(
                        session,
                        Event(
                            author="user" if content.role == "user"
                            else app.root_agent.name,
                            content=content,
                        ),
                    )
                async for event in runner.run_async(
                    user_id=requester_id, session_id=session.id,
                    new_message=message,
                    run_config=RunConfig(
                        max_llm_calls=settings.agent_max_llm_calls
                    ),
                ):
                    prompt_tokens, generated_tokens = _usage_from_event(event)
                    used_input += prompt_tokens
                    used_output += generated_tokens
                    for step in _steps_from_event(event, len(all_steps)):
                        await run_in_threadpool(append_step, team_id, run_id, step)
                        all_steps.append(step)
                        yield step
                if all_steps:
                    break
                logger.warning(
                    "empty turn from the model (attempt %d/%d), team=%s run=%s",
                    attempt, EMPTY_TURN_ATTEMPTS, team_id, run_id,
                )
        except Exception:
            await run_in_threadpool(
                _finish, team_id, run_id, "failed", used_input, used_output
            )
            raise
        reply = _reply_from_steps(all_steps)
        if not reply:
            # 🔴 A turn that produced nothing was being reported as a success.
            #
            # Measured 2026-08-31 against the live model: six of fourteen
            # consecutive turns came back with NO events at all — no tool
            # calls, no text, all_steps = 0 — and every one was stamped
            # status='done'. server/app.py only persists a reply `if reply`,
            # so nothing was written, nothing rendered, and the member who
            # asked Comrade a question got silence indistinguishable from a
            # hang. The run row said it went fine.
            #
            # Whatever makes the model return an empty candidate (a filtered
            # response, a transient upstream error ADK swallows) is a separate
            # question. This is the part that is ours: an empty turn is a
            # FAILED turn, and the member has to be told rather than left
            # watching an indicator disappear.
            #
            # 'failed' rather than a new status, and deliberately: from the
            # product's side "raised an exception" and "produced no answer"
            # are the same event — you asked and got nothing. The traceback
            # path still logs its own detail.
            await run_in_threadpool(
                _finish, team_id, run_id, "failed", used_input, used_output
            )
            yield {
                "type": "empty",
                "run_id": run_id,
                "detail": (
                    "Comrade had nothing to say that time — the model came"
                    " back empty. Nothing was changed. Try asking again."
                ),
            }
            return
        await run_in_threadpool(
            _finish, team_id, run_id, "done", used_input, used_output
        )
        yield {"type": "final", "run_id": run_id, "reply": reply}


async def run_turn(
    team_id: str,
    requester_id: str,
    user_text: str,
    trigger_type: str = "user",
    thread_type: str = "private",
    exclude_message_id: str | None = None,
) -> dict[str, Any]:
    """Batch form of stream_turn: drain it and return the collected result."""
    steps: list[dict[str, Any]] = []
    final: dict[str, Any] = {}
    async for item in stream_turn(
        team_id, requester_id, user_text, trigger_type,
        thread_type, exclude_message_id,
    ):
        if item.get("type") == "run":
            continue
        if item.get("type") == "final":
            final = item
            continue
        if item.get("type") == "busy":
            # The room lock refused this turn (§4.3). There is no run row and
            # no reply — surface it as itself rather than KeyError-ing on a
            # `final` frame that will never arrive.
            return {
                "run_id": None,
                "reply": "",
                "steps": [],
                "busy": item["detail"],
            }
        if item.get("type") == "empty":
            # Same trap, second cause: the model returned nothing, so there is
            # no `final` either and `final["run_id"]` below would raise. The
            # run row DOES exist here (and is now marked failed), so hand it
            # back — a caller that wants to look up what happened can.
            return {
                "run_id": item["run_id"],
                "reply": "",
                "steps": steps,
                "empty": item["detail"],
            }
        steps.append(item)
    return {"run_id": final["run_id"], "reply": final["reply"], "steps": steps}


def run_turn_sync(
    team_id: str,
    requester_id: str,
    user_text: str,
    trigger_type: str = "user",
    thread_type: str = "private",
    exclude_message_id: str | None = None,
) -> dict[str, Any]:
    """Blocking wrapper around run_turn for sync callers (the HTTP handler).

    Must not be called from within a running event loop — asyncio.run() creates
    a new loop and raises if one is already running.
    """
    return asyncio.run(run_turn(
        team_id, requester_id, user_text, trigger_type,
        thread_type, exclude_message_id,
    ))
