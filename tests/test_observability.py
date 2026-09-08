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
