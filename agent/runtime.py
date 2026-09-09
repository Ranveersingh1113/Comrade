"""Run a single agent turn and record it to agent_runs.

Conversation history is the messages table's job (source of truth); each turn
uses a fresh ADK runner whose in-memory session is REPLAYED from that table
(agent/history.py) before the new message lands, so the agent remembers the
thread it is standing in without a second session store.
The orchestrator (run_turn / run_turn_sync) is defined below the pure helpers.
"""
import asyncio
import json
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
from agent.effects import completed_effects, run_is_active
from agent.history import replay_for_turn, working_state_content
from agent.plan_tools import read_plan
from agent.repo_tools import connected_repo
from pipeline.parsers import spotlight
from shared.agent_runs import (
    append_step, finish_run, pause_for_permission, start_run, usage_so_far,
)
from shared.db import thread_lock
from shared.observability import bind, log_context
from shared.usage import claim_budget, claimed_tokens, finalize_usage
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


def _still_ours(team_id: str, run_id: str, worker_id: str | None) -> bool:
    """run_is_active with the worker fence, in a shape run_in_threadpool can
    call — it forwards no keyword arguments."""
    return run_is_active(team_id, run_id, worker_id=worker_id)


def _finish(
    team_id: str, run_id: str, status: str, used_input: int, used_output: int,
    worker_id: str | None = None, last_error: str | None = None,
) -> None:
    """finish_run with positional arguments — run_in_threadpool forwards no
    keywords, and the usage parameters are keyword-only so a caller cannot
    silently swap the two token counts."""
    try:
        finish_run(
            team_id, run_id, status,
            input_tokens=used_input, output_tokens=used_output, worker_id=worker_id,
            last_error=last_error,
        )
    except LookupError:
        # 🔴 The run already left `running` — it was cancelled, or recovery
        # took it. `finish_run` refuses that on purpose, so a worker's late
        # "failed" cannot overwrite the answer a member gave. But it RAISED,
        # and the settlement below never ran: the estimate stayed on the
        # team's bucket for the rest of the hour, charging them for the turn
        # they stopped. The status is not ours to write; the money still is.
        logger.info("run %s already finished elsewhere; settling usage", run_id)
    # Reconcile the estimate the turn reserved against what it actually cost.
    # HERE rather than on the success path, because a turn that failed still
    # paid for the prompt it was handed — and a turn that cost less than its
    # estimate must give the balance back or a team slowly loses budget it
    # never spent. finalize_usage is idempotent; this runs on every exit.
    # WITH OUR IDENTITY. A worker that lost its lease must not settle a run a
    # replacement finished — `finish_run` above already refused it, and
    # settling anyway is how the stale total became the durable one
    # (fix.md F43).
    finalize_usage(team_id, run_id, used_input + used_output, worker_id)


def _over_budget(team_id: str, run_id: str | None, spent: int) -> int | None:
    """What this run may spend in total, once it has spent more than that.

    Returns None to keep going, or the amount the run is limited to when it
    cannot claim any more.

    🔴 Admission reserved an ESTIMATE — 6,000 tokens, measured on a trivial
    turn — and then let the run make up to `agent_max_llm_calls` (20) model
    calls with nothing between them checking what they cost. A sweep that
    reads file after file carries the whole growing context into every call,
    so one admitted turn could spend several million tokens against a
    500,000-per-hour cap.

    🔴 And the first fix for that ASKED how much room the team had, which let
    every concurrent run treat the same headroom as its own: two turns with
    6,000 reservations were each told they could spend 494,000 of a 500,000
    cap. A shared budget cannot be divided by reading it. This CLAIMS instead,
    in chunks, through the same atomic conditional update admission uses — so
    two runs asking at once serialise, and only one takes the last slice.

    Nothing is read or claimed until a turn passes its own estimate, so an
    ordinary turn pays nothing for this.
    """
    if run_id is None or spent <= settings.agent_tokens_estimate:
        return None
    claimed = claimed_tokens(team_id, run_id)
    if claimed is None:
        return None                      # the team is uncapped
    # Claim in chunks until this run has covered what it has already spent.
    # Bounded so a wildly expensive call cannot spin here: each refusal is the
    # answer, and each grant moves `claimed` forward by a whole chunk.
    while spent > claimed:
        if not claim_budget(team_id, run_id):
            return claimed
        claimed += settings.agent_tokens_estimate
    return None


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


def _continuation_content(
    effects: list[dict[str, Any]], plan: dict[str, Any] | None = None,
) -> types.Content | None:
    """A resumed run's completed effects and the thread's unfinished plan,
    marked as untrusted context.

    Completed steps are dropped on purpose: what has to survive a restart is
    what is LEFT, and a finished step re-read as context is an invitation to
    do it again. The version travels with them because the model needs it to
    write the plan back without clobbering a concurrent run's edit.
    """
    record: dict[str, Any] = {}
    if effects:
        record["effects"] = effects
    if plan:
        record["plan"] = {
            "version": plan["version"],
            "remaining": [
                step for step in plan["steps"] if step.get("status") != "completed"
            ],
        }
    if not record:
        return None
    data = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return types.Content(
        role="user",
        parts=[types.Part(text=f"Continuation record (data): {spotlight(data)}")],
    )


def _permission_wait(step: dict[str, Any]) -> bool:
    return step.get("type") == "tool_result" and bool(
        isinstance(step.get("response"), dict) and step["response"].get("consent_id")
    )


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
# 🔴 RE-MEASURED 2026-09-09 (fix.md F52), and the rate has moved a long way.
# The paragraph above sized this at three because a 1-in-8 empty rate makes a
# third failure ~1 in 500. It is not 1 in 8 any more. Asking the same ordinary
# question through `stream_turn` 24 times, in two runs:
#
#     42 model calls, 19 of them empty          ~45% per call
#     1 turn in 12 exhausted all three attempts  ~8% of turns answered nothing
#
# At 45%, three attempts is ~1 in 11, not 1 in 500 — which is the review's
# observation exactly, and needs no explanation beyond arithmetic. Six gets it
# back under 1%. An empty call generates no output tokens, so the cost of the
# extra attempts is prompt tokens and latency, not answers.
#
# 🔴 This is a RECALIBRATION, not a root cause. Why the model returns an empty
# candidate is still not established; two plausible causes were tested and are
# not it:
#
#   * the experimental JSON_SCHEMA_FOR_FUNC_DECL declaration path — disabling
#     it made every call empty, 36/36, so it is load-bearing, not the fault;
#   * request pacing — 8 seconds between turns left the per-call rate at 43%,
#     so this is not rate limiting.
#
# If a later reader finds this number climbing again, the rate is the thing to
# measure, and raising the constant is not the answer twice.
EMPTY_TURN_ATTEMPTS = 6


async def stream_turn(
    team_id: str,
    requester_id: str,
    user_text: str,
    *,
    thread_id: str,
    trigger_type: str = "user",
    exclude_message_id: str | None = None,
    lock_held: bool = False,
    run_id: str | None = None,
    worker_id: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run one turn, yielding each step as it happens and recording it.

    team_id / requester_id are SERVER-BOUND here and injected into ADK session
    state — the model receives them via state, never as tool arguments.

    thread_id says WHICH conversation this is; history is thread-scoped.
    exclude_message_id is the member's just-persisted message: the server
    writes it before calling us, so without this the model gets it twice.

    Yields {"type": "run", ...} first, then one dict per step, then
    {"type": "final", ...}. run_turn() below consumes this, so the
    orchestration exists once rather than twice.
    """
    # One agent turn at a time per thread. Different threads remain parallel.
    # The HTTP layer may already hold this lock before it records the input,
    # which prevents a rejected busy turn becoming later history.
    with ExitStack() as stack:
        # Every line this turn produces — here, in the tools, in the DB
        # helpers — carries the ids an operator filters on. `run_id` is bound
        # later, because the run row does not exist yet.
        stack.enter_context(log_context(
            team_id=team_id, thread_id=thread_id, worker_id=worker_id,
        ))
        if not lock_held and not stack.enter_context(thread_lock(thread_id)):
            yield {
                "type": "busy",
                "detail": (
                    "Comrade is working on someone else's question in this"
                    " thread. Yours is next — send it again in a moment."
                ),
            }
            return

        if run_id is None:
            run_id = await run_in_threadpool(
                start_run, team_id, requester_id, thread_id, exclude_message_id,
                trigger_type, user_text[:200],
            )
        bind(run_id=run_id)
        yield {"type": "run", "run_id": run_id}

        message = types.Content(role="user", parts=[types.Part(text=user_text)])
        all_steps: list[dict[str, Any]] = []
        # Outside the retry loop on purpose: a retried empty turn paid
        # for its prompt every attempt, and that is the number worth
        # having.
        # 🔴 (fix.md F42) SEEDED, not zeroed. A run resumed after a permission
        # wait carries what its earlier segments spent; starting from zero made
        # `finish_run` — which writes these columns absolutely — record only
        # the last segment, while the claimed allocation stayed cumulative.
        used_input, used_output = (
            await run_in_threadpool(usage_so_far, team_id, run_id)
            if run_id else (0, 0)
        )
        # Inside the try: a failed history read must close the run row too, not
        # leave it 'running' forever.
        try:
            # The summary and the replay have to MEET: everything since the
            # summary, not the last N messages, or up to
            # MIN_COMPACT_MESSAGES-1 of them sit in neither window
            # (fix.md F16). Composed in one place so there is one answer to
            # "what does a turn see".
            history = await run_in_threadpool(
                replay_for_turn, team_id, requester_id, thread_id,
                settings.agent_history_turns, exclude_message_id,
            )
            # 🔴 The window WAS the memory. A constraint stated a hundred
            # messages ago was invisible to this turn, so the agent proposed
            # what the team had already ruled out and somebody had to say it
            # again. The thread's established state goes in FIRST, ahead of
            # the recent messages, and is datamarked because a summary is
            # written from member text.
            established = await run_in_threadpool(
                working_state_content, team_id, thread_id,
            )
            if established is not None:
                history = [established, *history]
            effects = await run_in_threadpool(completed_effects, team_id, run_id)
            plan = await run_in_threadpool(read_plan, team_id, thread_id)
            continuation = _continuation_content(effects, plan)
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
                        "thread_id": thread_id,
                        "agent_run_id": run_id,
                        # Carried so a tool can prove the run is still THIS
                        # worker's before it does anything outside the process.
                        "worker_id": worker_id,
                        "steering_message_ids": [],
                        # Server-bound like the two above. The repo tools
                        # derive the checkout path from these; the model names
                        # neither, so it cannot ask to work in another team's
                        # tree or another team's repository.
                        "repo_full_name": repo,
                        # "Has Comrade run what it just wrote." repo_edit
                        # bumps the first, a repo_run that exited 0 stamps it
                        # into the second, and repo_propose_pr refuses while
                        # they disagree.
                        #
                        # Seeded HERE for the same reason as the three above:
                        # the model must not be able to name either key, or it
                        # could vouch for its own unrun work. Starting fresh
                        # each turn is correct rather than incidental — the
                        # checkout is reset from the remote every turn too, so
                        # a pass measured last turn was measured on a tree
                        # that no longer exists.
                        "repo_edit_generation": 0,
                        "repo_verified_generation": None,
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
                if continuation is not None:
                    await runner.session_service.append_event(
                        session, Event(author="user", content=continuation)
                    )
                async for event in runner.run_async(
                    user_id=requester_id, session_id=session.id,
                    new_message=message,
                    run_config=RunConfig(
                        max_llm_calls=settings.agent_max_llm_calls
                    ),
                ):
                    # 🔴 (fix.md F43) COUNTED FIRST. This event is a response
                    # that has already been generated and already been paid
                    # for. The ownership check used to come first and return,
                    # so a stop landing during the first model call finalized
                    # ZERO tokens and refunded the whole estimate — the team
                    # got the bill from the provider and a refund from us.
                    prompt_tokens, generated_tokens = _usage_from_event(event)
                    used_input += prompt_tokens
                    used_output += generated_tokens
                    # Between steps is where a stop becomes real. An in-flight
                    # model call cannot be taken back, but everything after it
                    # can, and this is the boundary the loop actually passes
                    # through — before the next tool and before the next call.
                    if run_id and not await run_in_threadpool(
                        _still_ours, team_id, run_id, worker_id
                    ):
                        await run_in_threadpool(
                            _finish, team_id, run_id, "cancelled",
                            used_input, used_output, worker_id,
                        )
                        yield {"type": "cancelled", "run_id": run_id}
                        return
                    for step in _steps_from_event(event, len(all_steps)):
                        await run_in_threadpool(
                            append_step, team_id, run_id, step, worker_id=worker_id
                        )
                        all_steps.append(step)
                        yield step
                        if _permission_wait(step):
                            # With what it has spent so far: the next segment
                            # resumes from these, and without them the earlier
                            # ones are overwritten (fix.md F42).
                            await run_in_threadpool(
                                pause_for_permission, team_id, run_id,
                                worker_id, used_input, used_output,
                            )
                            yield {"type": "waiting_for_permission", "run_id": run_id}
                            return
                    # AFTER this event's steps are recorded and sent, not
                    # before: the call that just happened cannot be taken back
                    # and has already been paid for, so throwing away what it
                    # produced would cost the team the money AND the work. The
                    # next nineteen calls are what this stops — the same
                    # boundary, and the same reasoning, as the cancellation
                    # check above.
                    allowance = await run_in_threadpool(
                        _over_budget, team_id, run_id, used_input + used_output,
                    )
                    if allowance is not None:
                        detail = (
                            "This turn used more than the team's remaining"
                            f" token budget for the hour ({allowance:,}), so"
                            " Comrade stopped part-way. What it managed before"
                            " stopping is above. Ask again next hour, or raise"
                            " AGENT_TOKENS_PER_HOUR."
                        )
                        await run_in_threadpool(
                            _finish, team_id, run_id, "failed",
                            used_input, used_output, worker_id, detail,
                        )
                        yield {
                            "type": "over_budget", "run_id": run_id,
                            "detail": detail,
                        }
                        return
                if all_steps:
                    break
                logger.warning(
                    "empty turn from the model (attempt %d/%d), team=%s run=%s",
                    attempt, EMPTY_TURN_ATTEMPTS, team_id, run_id,
                )
        except Exception:
            await run_in_threadpool(
                _finish, team_id, run_id, "failed", used_input, used_output, worker_id
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
            # 🔴 "Nothing was changed" was true for one of these two cases and
            # asserted for both. Found 2026-09-04 by the four-person scenario:
            # a turn proposed an action, wrote a consent row, said nothing,
            # and told the member nothing had changed. An approval was waiting
            # in the thread while it said so.
            #
            # The retry above is correctly unavailable once a tool has run —
            # re-asking would run it twice. That is precisely why this message
            # has to carry the weight: it is the only thing the member gets,
            # and it was describing the other case. Naming the tools is what
            # makes it actionable — "check what it did" is useless without
            # saying what it did.
            tools_run = [
                s["tool"] for s in all_steps
                if s.get("type") == "tool_call" and s.get("tool")
            ]
            if tools_run:
                detail = (
                    "Comrade attempted "
                    + ", ".join(dict.fromkeys(tools_run))
                    + " and then stopped without saying anything. Check for"
                    " pending approvals or new activity before asking again;"
                    " retrying may repeat a successful action."
                )
            else:
                detail = (
                    "Comrade had nothing to say that time — the model came"
                    " back empty. Nothing was changed. Try asking again."
                )
            # 🔴 Written down, not only yielded. The durable queue moved the
            # consumer of this generator from the browser to the worker, so
            # the member now reads the RUN ROW (server/app.py:_run_frames).
            # An explanation that lives only in the frame reaches nobody, and
            # the empty turn goes back to looking exactly like a hang — the
            # regression this whole path exists to prevent.
            await run_in_threadpool(
                _finish, team_id, run_id, "failed", used_input, used_output,
                worker_id, detail,
            )
            yield {"type": "empty", "run_id": run_id, "detail": detail}
            return
        await run_in_threadpool(
            _finish, team_id, run_id, "done", used_input, used_output, worker_id
        )
        yield {"type": "final", "run_id": run_id, "reply": reply}


#: Frames that end a turn WITHOUT a `final`. Each leaves a run row (except
#: `busy`, which never had one), so `run_turn` reports the outcome under its
#: own name rather than raising on a `final` that will never arrive.
_STOPS_WITHOUT_A_FINAL = frozenset({
    "empty", "cancelled", "over_budget", "waiting_for_permission",
})


async def run_turn(
    team_id: str,
    requester_id: str,
    user_text: str,
    *,
    thread_id: str,
    trigger_type: str = "user",
    exclude_message_id: str | None = None,
    lock_held: bool = False,
    run_id: str | None = None,
    worker_id: str | None = None,
) -> dict[str, Any]:
    """Batch form of stream_turn: drain it and return the collected result."""
    steps: list[dict[str, Any]] = []
    final: dict[str, Any] = {}
    async for item in stream_turn(
        team_id, requester_id, user_text, thread_id=thread_id,
        trigger_type=trigger_type, exclude_message_id=exclude_message_id,
        lock_held=lock_held, run_id=run_id, worker_id=worker_id,
    ):
        if item.get("type") == "run":
            continue
        if item.get("type") == "final":
            final = item
            continue
        if item.get("type") == "busy":  # no run row at all — see below
            # The room lock refused this turn (§4.3). There is no run row and
            # no reply — surface it as itself rather than KeyError-ing on a
            # `final` frame that will never arrive.
            return {
                "run_id": None,
                "reply": "",
                "steps": [],
                "busy": item["detail"],
            }
        if item.get("type") in _STOPS_WITHOUT_A_FINAL:
            # 🔴 This used to be a branch per frame, and every new way for a
            # turn to stop early re-opened the same hole: no `final` arrives,
            # so `final["run_id"]` raised KeyError. Empty was the second cause
            # and got its own branch; cancellation (T11) and the budget brake
            # were the third and fourth and got none. The agent worker drives
            # turns through `run_turn_sync`, where that exception is a crashed
            # worker rather than a handled outcome.
            #
            # The run row exists on all of these — hand it back, so a caller
            # that wants to look up what happened can.
            outcome = {"run_id": item["run_id"], "reply": "", "steps": steps}
            outcome[item["type"]] = item.get("detail", True)
            return outcome
        steps.append(item)
    # A `final` that never came is a frame this function has not been taught
    # about. Report the run truthfully rather than raising on the way out.
    return {"run_id": final.get("run_id", run_id), "reply": final.get("reply", ""),
            "steps": steps}


def run_turn_sync(
    team_id: str,
    requester_id: str,
    user_text: str,
    *,
    thread_id: str,
    trigger_type: str = "user",
    exclude_message_id: str | None = None,
    lock_held: bool = False,
    run_id: str | None = None,
    worker_id: str | None = None,
) -> dict[str, Any]:
    """Blocking wrapper around run_turn for sync callers (the HTTP handler).

    Must not be called from within a running event loop — asyncio.run() creates
    a new loop and raises if one is already running.
    """
    return asyncio.run(run_turn(
        team_id, requester_id, user_text, thread_id=thread_id,
        trigger_type=trigger_type, exclude_message_id=exclude_message_id,
        lock_held=lock_held, run_id=run_id, worker_id=worker_id,
    ))
