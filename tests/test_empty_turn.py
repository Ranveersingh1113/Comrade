"""🔴 A turn that produced nothing was being reported as a success.

Found 2026-08-31 while running D6's gates. The e2e private-thread journey was
failing intermittently and I twice wrote it off — once as model
nondeterminism, once as a timeout too tight. Both wrong. The runs table says
what it actually was:

    status | text_steps | all_steps
    done   |     0      |     0        <- six of fourteen consecutive turns
    done   |     1      |     3

No events at all came back from the runner: no tool calls, no text. Because
server/app.py persists a reply only `if reply`, nothing was written and
nothing rendered — a member asked Comrade a question and got silence
indistinguishable from a hang, while the run row recorded a success.

Whatever makes the model return an empty candidate is a separate question.
This pins the part that is ours: an empty turn is a FAILED turn, and it has to
say so.
"""
import asyncio

import psycopg
import pytest

from shared.config import settings
from tests._seed import A1, TEAM_A


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def silent_model(monkeypatch):
    """A runner that yields no events — exactly what was observed live."""
    async def _fake_run(*_a, **_k):
        return
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr("agent.runtime.Runner.run_async", _fake_run)


def _frames(thread_type="private"):
    from agent.runtime import stream_turn

    thread_id = _thread_id(thread_type)

    async def _drain():
        return [
            f async for f in stream_turn(
                TEAM_A, A1, "what is open?", thread_id=thread_id
            )
        ]

    return asyncio.run(_drain())


def _thread_id(thread_type="private") -> str:
    conn = psycopg.connect(settings.comrade_db_url_admin)
    try:
        if thread_type == "group":
            row = conn.execute(
                "select id from public.threads where team_id=%s and title='General'",
                (TEAM_A,),
            ).fetchone()
        else:
            row = conn.execute(
                "select id from public.threads where team_id=%s"
                " and legacy_thread_owner_id=%s",
                (TEAM_A, A1),
            ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0])


def test_an_empty_turn_tells_the_member(seeded, silent_model):
    """The frame that was missing. Without it the screen shows nothing at all
    — the typing indicator simply stops, which is what a hang looks like."""
    frames = _frames()
    assert [f["type"] for f in frames] == ["run", "empty"]
    assert "empty" in frames[-1]["detail"]
    assert "Nothing was changed" in frames[-1]["detail"], (
        "the member needs to know the silence cost them nothing"
    )


def test_an_empty_turn_is_not_recorded_as_done(seeded, silent_model, admin):
    """The half that made this invisible for so long.

    Marked 'failed' rather than given a status of its own, deliberately: from
    the product's side "raised an exception" and "produced no answer" are the
    same event — you asked and got nothing.
    """
    frames = _frames()
    run_id = frames[0]["run_id"]
    assert admin.execute(
        "select status from public.agent_runs where id=%s", (run_id,)
    ).fetchone()[0] == "failed"


def test_the_run_row_is_still_closed(seeded, silent_model, admin):
    """A run left 'running' forever is the other way to lose a turn."""
    frames = _frames()
    assert admin.execute(
        "select finished_at is not null from public.agent_runs where id=%s",
        (frames[0]["run_id"],),
    ).fetchone()[0] is True


def test_run_turn_does_not_raise_on_an_empty_turn(seeded, silent_model):
    """run_turn reads final["run_id"], and there is no final frame here.

    The busy path already carried this exact trap and a comment about it; the
    empty path is its second cause and would have raised KeyError inside the
    HTTP handler — turning a silent non-answer into a 500.
    """
    from agent.runtime import run_turn

    result = asyncio.run(run_turn(
        TEAM_A, A1, "what is open?", thread_id=_thread_id()
    ))
    assert result["reply"] == ""
    assert result["run_id"] is not None, "the run row exists and is worth returning"
    assert "empty" in result


def test_a_normal_turn_is_untouched(seeded, monkeypatch, admin):
    """The guard must not fire on a turn that actually said something."""
    from unittest.mock import MagicMock

    from agent.runtime import stream_turn

    part = MagicMock()
    part.function_call = None
    part.function_response = None
    part.text = "Two tasks are open."
    event = MagicMock()
    event.content.parts = [part]

    async def _fake_run(*_a, **_k):
        yield event

    monkeypatch.setattr("agent.runtime.Runner.run_async", _fake_run)

    async def _drain():
        return [
            f async for f in stream_turn(
                TEAM_A, A1, "what is open?", thread_id=_thread_id()
            )
        ]

    frames = asyncio.run(_drain())
    assert [f["type"] for f in frames] == ["run", "text", "final"]
    assert frames[-1]["reply"] == "Two tasks are open."
    assert admin.execute(
        "select status from public.agent_runs where id=%s", (frames[0]["run_id"],)
    ).fetchone()[0] == "done"


def test_whitespace_only_is_still_empty(seeded, monkeypatch):
    """_reply_from_steps strips, so a model that answers with a newline
    produces an empty reply and must take the same path — otherwise the member
    gets a blank message bubble instead of an explanation."""
    from unittest.mock import MagicMock

    part = MagicMock()
    part.function_call = None
    part.function_response = None
    part.text = "   \n  "
    event = MagicMock()
    event.content.parts = [part]

    async def _fake_run(*_a, **_k):
        yield event

    monkeypatch.setattr("agent.runtime.Runner.run_async", _fake_run)
    assert _frames()[-1]["type"] == "empty"


# ---------------------------------------------------------------------------
# The root cause, and the retry it justifies
# ---------------------------------------------------------------------------

def test_an_empty_turn_is_retried_before_giving_up(seeded, monkeypatch):
    """🔴 ROOT CAUSE, instrumented 2026-09-01.

    Gemini intermittently returns a candidate with an EMPTY parts list and a
    perfectly normal finish_reason of STOP — no safety block, no truncation,
    no error, no exception:

        finish_reason STOP · error_code None · n_parts 0
        prompt_token_count 3180 · total_token_count 3180   (zero output)

    It was handed 3180 tokens of prompt and generated none. Nothing downstream
    can tell that apart from a model with nothing to say, which is how it
    reached the member as silence. Measured 1 in 8 on a real prompt.

    So: ask again. The second attempt sees the same history and usually
    answers.
    """
    from unittest.mock import MagicMock

    calls = {"n": 0}

    def _reply(text):
        part = MagicMock()
        part.function_call = None
        part.function_response = None
        part.text = text
        ev = MagicMock()
        ev.content.parts = [part]
        return ev

    async def _fake_run(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            # The observed shape: an event whose content has NO parts at all.
            ev = MagicMock()
            ev.content.parts = []
            yield ev
        else:
            yield _reply("Two tasks are open.")

    monkeypatch.setattr("agent.runtime.Runner.run_async", _fake_run)
    frames = _frames()

    assert calls["n"] == 2, "the empty response was not retried"
    assert [f["type"] for f in frames] == ["run", "text", "final"]
    assert frames[-1]["reply"] == "Two tasks are open."


def test_a_turn_that_used_a_tool_is_never_retried(seeded, monkeypatch):
    """The line that makes retrying safe at all.

    An empty turn has no side effects BY DEFINITION — no tool ran, nothing was
    written, nothing was yielded. That is the entire argument for re-asking.
    A turn that called a tool and THEN went quiet is a different animal: it
    already changed something, and asking again would run the tool twice —
    two consent rows, two nudges, two of whatever it did.

    So the guard is "produced no steps", not "produced no reply".
    """
    from unittest.mock import MagicMock

    calls = {"n": 0}

    async def _fake_run(*_a, **_k):
        calls["n"] += 1
        part = MagicMock()
        part.function_call = MagicMock(name="fc")
        part.function_call.name = "team_get_state"
        part.function_call.args = {}
        part.function_response = None
        part.text = None
        ev = MagicMock()
        ev.content.parts = [part]
        yield ev

    monkeypatch.setattr("agent.runtime.Runner.run_async", _fake_run)
    frames = _frames()

    assert calls["n"] == 1, "a turn with a tool call was retried"
    # Still no reply, so the member is still told — the retry is an
    # improvement on the empty case, not a replacement for saying so.
    assert frames[-1]["type"] == "empty"


def test_it_gives_up_rather_than_retrying_forever(seeded, monkeypatch):
    """A persistently empty model is something systematic, not bad luck, and
    the member's budget is not the place to discover which. Bounded, and the
    member is still told at the end."""
    from agent.runtime import EMPTY_TURN_ATTEMPTS

    calls = {"n": 0}

    async def _always_empty(*_a, **_k):
        calls["n"] += 1
        return
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr("agent.runtime.Runner.run_async", _always_empty)
    frames = _frames()

    assert calls["n"] == EMPTY_TURN_ATTEMPTS
    assert frames[-1]["type"] == "empty"


def test_the_retry_starts_from_a_clean_session(seeded, monkeypatch):
    """ADK appends what it produced to the session as it goes, so retrying on
    the same one would hand the model its own empty candidate as context —
    asking it to continue from the failure it just had."""
    from unittest.mock import MagicMock

    sessions = []
    calls = {"n": 0}

    async def _fake_run(*_a, session_id=None, **_k):
        calls["n"] += 1
        sessions.append(session_id)
        if calls["n"] == 1:
            ev = MagicMock()
            ev.content.parts = []
            yield ev
        else:
            part = MagicMock()
            part.function_call = None
            part.function_response = None
            part.text = "second time lucky"
            ev = MagicMock()
            ev.content.parts = [part]
            yield ev

    monkeypatch.setattr("agent.runtime.Runner.run_async", _fake_run)
    _frames()
    assert len(sessions) == 2
    assert sessions[0] != sessions[1], "the retry reused the failed session"


def test_a_silent_turn_that_ran_a_tool_does_not_claim_nothing_changed(
    seeded, monkeypatch
):
    """🔴 The message was true for one case and asserted for both.

    Found 2026-09-04 by the four-person scenario. Two of six live turns came
    back HTTP 200 with an empty reply, and both had run tools first:

        run       | steps | text_steps | status | side effect
        0368ae0a  |   4   |     0      | failed | -
        2386b070  |   6   |     0      | failed | 3 pending task_create rows

    The second one called team_propose_batch, wrote three consent rows, said
    nothing, and told the member "Nothing was changed." Three approvals were
    sitting in the queue at the time.

    The retry above is correctly unavailable here — re-asking would run the
    tool twice. That is exactly why the MESSAGE has to carry the weight: it is
    the only thing the member gets, and it was describing the other case.
    "Nothing was changed" is a claim about side effects that nothing checked,
    which is the same shape as the success-report bug this whole file exists
    for, one layer along.
    """
    from unittest.mock import MagicMock

    async def _fake_run(*_a, **_k):
        part = MagicMock()
        part.function_call = MagicMock(name="fc")
        part.function_call.name = "team_propose_batch"
        part.function_call.args = {}
        part.function_response = None
        part.text = None
        ev = MagicMock()
        ev.content.parts = [part]
        yield ev

    monkeypatch.setattr("agent.runtime.Runner.run_async", _fake_run)
    detail = _frames()[-1]["detail"]

    assert "Nothing was changed" not in detail, (
        "a turn that ran a tool told the member nothing was changed. The tool"
        f" had already run. detail was: {detail!r}"
    )
    assert "team_propose_batch" in detail, (
        "the member is not told WHICH tool ran, so they cannot go look for"
        f" what it did. detail was: {detail!r}"
    )
    assert "attempted" in detail and "already ran" not in detail, detail
