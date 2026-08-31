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

    async def _drain():
        return [
            f async for f in stream_turn(
                TEAM_A, A1, "what is open?", thread_type=thread_type
            )
        ]

    return asyncio.run(_drain())


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

    result = asyncio.run(run_turn(TEAM_A, A1, "what is open?"))
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
            f async for f in stream_turn(TEAM_A, A1, "what is open?")
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
