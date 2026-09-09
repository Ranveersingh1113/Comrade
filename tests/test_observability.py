"""Finding one member's failed turn in a log, without the log leaking it.

🔴 THE DEFECTS.

1. Nothing correlated. Every log line was prose, and the ids that appear in
   one are whichever ones that call site happened to interpolate. An operator
   handed "Comrade did nothing when I asked at 14:32" had no field to filter
   on — not the run, not the thread, not the team.

2. Redaction was a convention, and a convention is what the one careless line
   ignores. `shared/errors.py` cleans an error a caller remembers to pass
   through it; a log is the copy that leaves the building, and it needed a
   guarantee rather than a habit.
"""
import asyncio
import logging

import pytest

from shared.observability import bind, configure, log_context


@pytest.fixture
def captured():
    """A handler with the real formatter and filter, so this tests what ships
    rather than a reconstruction of it."""
    records: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    sink = _Sink()
    configure(sink)
    logger = logging.getLogger("comrade.test.observability")
    logger.addHandler(sink)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    yield logger, records
    logger.removeHandler(sink)


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------

def test_a_line_inside_a_run_carries_the_run(captured):
    """🔴 It carried whatever the call site interpolated, which is nothing to
    filter on."""
    logger, records = captured

    with log_context(team_id="t-1", run_id="r-1", thread_id="th-1"):
        logger.info("the model returned nothing")

    line = records[-1]
    assert "run_id=r-1" in line
    assert "thread_id=th-1" in line
    assert "team_id=t-1" in line


def test_the_context_does_not_outlive_its_block(captured):
    """A worker slot handles one team's job and then another's. Context that
    leaked between them would attribute one team's failure to another, which
    is worse than having none."""
    logger, records = captured

    with log_context(team_id="t-1", job_id="j-1"):
        logger.info("working")
    logger.info("idle")

    assert "job_id=j-1" not in records[-1]


def test_context_nests(captured):
    """A job runs inside a worker slot; a turn runs inside a thread."""
    logger, records = captured

    with log_context(team_id="t-1"):
        with log_context(run_id="r-1"):
            logger.info("inner")
        logger.info("outer")

    assert "team_id=t-1" in records[-2] and "run_id=r-1" in records[-2]
    assert "team_id=t-1" in records[-1] and "run_id=" not in records[-1]


def test_bind_adds_to_the_current_context(captured):
    """A run id does not exist until the run row does, which is after the
    turn's context has already been opened."""
    logger, records = captured

    with log_context(team_id="t-1"):
        bind(run_id="r-9")
        logger.info("started")

    assert "run_id=r-9" in records[-1]


def test_a_line_with_no_context_still_logs(captured):
    """Most lines are not inside a turn, and they must not become unreadable
    or, worse, an exception inside the logger."""
    logger, records = captured

    logger.info("worker up")

    assert "worker up" in records[-1]


# ---------------------------------------------------------------------------
# Redaction, as a guarantee rather than a habit
# ---------------------------------------------------------------------------

def test_a_credential_never_reaches_the_log_even_if_a_caller_forgets(captured):
    """🔴 The point. `shared/errors.py` cleans what a caller remembers to pass
    through it; this catches the line nobody thought about."""
    logger, records = captured

    logger.warning("clone failed: %s",
                   "https://x-access-token:ghs_0123456789abcdefghij@github.com/a/b")

    assert "ghs_0123456789abcdefghij" not in records[-1]


def test_a_failing_row_never_reaches_the_log_either(captured):
    logger, records = captured

    logger.error(
        "insert failed: %s",
        'violates check constraint "c"\n'
        "DETAIL:  Failing row contains (1, the staging password is hunter2).",
    )

    assert "hunter2" not in records[-1]


def test_the_message_survives_redaction(captured):
    logger, records = captured

    logger.info("queued %d compaction(s)", 3)

    assert "queued 3 compaction(s)" in records[-1]


def test_an_exception_traceback_is_redacted_too(captured):
    """`logger.exception` appends the traceback, and the exception's own
    message is in it — which is exactly where a DSN ends up."""
    logger, records = captured

    try:
        raise RuntimeError(
            "connect: postgresql://comrade_agent:s3cr3t@db:5432/postgres"
        )
    except RuntimeError:
        logger.exception("could not reach the database")

    assert "s3cr3t" not in records[-1]


# ---------------------------------------------------------------------------
# The services have to actually use it
# ---------------------------------------------------------------------------

def test_every_service_installs_the_redacting_handler():
    """🔴 Both workers called `logging.basicConfig`, which installs a handler
    with neither the formatter nor the filter. A redactor nothing is wired to
    is a module, not a guarantee."""
    import inspect

    from agent import worker as agent_worker
    from pipeline import worker as pipeline_worker
    import server.app as api

    for module in (agent_worker, pipeline_worker, api):
        source = inspect.getsource(module)
        assert "logging.basicConfig" not in source, (
            f"{module.__name__} installs a handler that does not redact"
        )
        assert "observability.setup" in source or "setup(" in source, (
            f"{module.__name__} never installs the redacting handler"
        )


def test_a_turn_binds_its_run_so_the_lines_can_be_found():
    """The correlation half, where it matters most: an operator handed "it did
    nothing when I asked at 14:32" needs a field to filter on."""
    import inspect

    from agent import runtime

    source = inspect.getsource(runtime.stream_turn)
    assert "log_context" in source, (
        "a turn is the unit an operator debugs and it opens no context"
    )

# ---------------------------------------------------------------------------
# F52/F53 — whose context is restored, and by whom
# ---------------------------------------------------------------------------
#
# 🔴 THE FIRST FIX WAS WRONG AND ITS TESTS COULD NOT SEE IT (fix.md F53).
#
# `stream_turn` holds `log_context` open across `yield`. F52 found that
# finalising it in another task raised — a Token may only be reset where it was
# created — and replaced the token with save-and-restore-by-value. That removed
# the exception and introduced a silent bug: `set()` writes into whichever task
# closes the generator, so the DRIVER kept the finished turn's identifiers and
# the CLOSER inherited them.
#
# The tests written with it asserted the outer context AFTER `asyncio.run`
# returned. That boundary copies the context, so they were looking at something
# the defect could not touch, and they passed.
#
# So every assertion below is made INSIDE the task under test, and each names
# whose context it is checking.

def _team() -> str:
    from shared.observability import _context

    return _context.get().get("team_id", "<none>")


async def _turn(steps=("run", "final")):
    """The shape of stream_turn: correlation held open across yields."""
    with log_context(team_id="turn-team"):
        for step in steps:
            yield step


def test_the_driver_gets_its_own_context_back_after_an_early_return():
    """🔴 THE DEFECT. Take one frame, stop early, and the driving task was left
    carrying `turn-team` — a finished turn's identifiers on every later line it
    logged, which is the mis-attribution log_context exists to prevent.

    `aclosing` is what makes this true: returning out of an `async for` does not
    close the generator, and whoever closed it later was not this task.
    """
    from contextlib import aclosing

    seen = {}

    async def driver():
        with log_context(team_id="caller-team"):
            async with aclosing(_turn()) as turn:
                async for _ in turn:
                    break            # the early exit every terminal frame takes
            seen["after"] = _team()

    asyncio.run(driver())

    assert seen["after"] == "caller-team", seen


def test_a_concurrent_task_keeps_its_own_context():
    """🔴 THE OTHER HALF. Restoring by value wrote the driver's previous
    mapping into the CLOSING task, so an unrelated task ended up correlated to
    someone else's team."""
    from contextlib import aclosing

    seen = {}

    async def other():
        with log_context(team_id="other-team"):
            await asyncio.sleep(0)
            seen["other"] = _team()

    async def driver():
        with log_context(team_id="caller-team"):
            task = asyncio.create_task(other())
            async with aclosing(_turn()) as turn:
                async for _ in turn:
                    break
            await task
            seen["driver"] = _team()

    asyncio.run(driver())

    assert seen == {"other": "other-team", "driver": "caller-team"}, seen


def test_normal_completion_restores_the_driver():
    """The path that always worked, kept: exhausting the generator."""
    from contextlib import aclosing

    seen = {}

    async def driver():
        with log_context(team_id="caller-team"):
            async with aclosing(_turn()) as turn:
                async for _ in turn:
                    pass
            seen["after"] = _team()

    asyncio.run(driver())

    assert seen["after"] == "caller-team", seen


def test_cancellation_restores_the_driver():
    """And the path a stopped turn takes. The generator is closed by the
    unwinding `aclosing`, in this task, before the cancellation propagates."""
    from contextlib import aclosing

    seen = {}

    async def driver():
        with log_context(team_id="caller-team"):
            try:
                async with aclosing(_turn()) as turn:
                    async for _ in turn:
                        raise asyncio.CancelledError
            except asyncio.CancelledError:
                pass
            seen["after"] = _team()

    asyncio.run(driver())

    assert seen["after"] == "caller-team", seen


def test_run_turn_closes_the_turn_it_drove():
    """The production consumer, not a stand-in for it.

    `run_turn` returns from inside its `async for` on every early frame. This
    replaces `stream_turn` with one that holds correlation open the same way and
    ends on a `busy` frame — an early return — then checks the context of the
    task that drove it.
    """
    import agent.runtime as runtime

    seen = {}

    async def fake_stream_turn(*args, **kwargs):
        with log_context(team_id="turn-team"):
            yield {"type": "run", "run_id": "r1"}
            yield {"type": "busy", "detail": "someone else is asking"}
            yield {"type": "final", "run_id": "r1", "reply": "unreachable"}

    async def driver():
        with log_context(team_id="caller-team"):
            result = await runtime.run_turn(
                "team", "user", "hello", thread_id="thread")
            seen["result"] = result
            seen["after"] = _team()

    original = runtime.stream_turn
    runtime.stream_turn = fake_stream_turn
    try:
        asyncio.run(driver())
    finally:
        runtime.stream_turn = original

    assert seen["result"]["busy"] == "someone else is asking", seen["result"]
    assert seen["after"] == "caller-team", (
        "run_turn returned out of its loop and left the turn's correlation"
        f" attached to the task that drove it: {seen}"
    )


def test_closing_a_turn_from_another_task_is_refused_loudly():
    """The property that makes `reset(token)` the right instrument, and the one
    the F52 fix traded away.

    Ownership is the contract: whoever drove the generator closes it. A caller
    that breaks it gets a ValueError out of the finaliser. That is not pleasant,
    and it is much better than the alternative it replaced — restoring by value
    breaks the same contract SILENTLY, leaving the driver correlated to a
    finished turn and the closer correlated to someone else's team.

    A test that asserts an exception is usually a smell. Here it is the
    regression guard: without it, swapping the token back for a value assignment
    passes everything else in this file.
    """
    import pytest as _pytest

    outcome = {}

    async def driver():
        gen = _turn()
        await gen.__anext__()

        async def closer():
            await gen.aclose()

        try:
            await asyncio.create_task(closer())
        except ValueError as exc:
            outcome["raised"] = str(exc)

    asyncio.run(driver())

    assert "different Context" in outcome.get("raised", ""), (
        "closing a turn from another task did not announce itself, so the"
        " driver and the closer are both quietly carrying the wrong context"
    )
