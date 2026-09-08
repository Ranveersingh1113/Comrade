"""What a failure is allowed to say about the thing that failed.

🔴 THE DEFECT. Every failure path recorded `str(exc)` verbatim — into
`jobs.last_error`, into `agent_runs.last_error`, and into the log. A database
error is not a short sentence: Postgres attaches `DETAIL: Failing row contains
(...)`, which is THE ROW — every column of it, in order. So a constraint
violation while compiling a document or writing a message wrote that content
into `jobs.last_error`, a column `comrade_control` can read across every team,
and into a log that is shipped somewhere else again.

This is the same shape as the defect T26 closed one layer down: the queue was
carefully denied a team's content and the queue's ERROR column was handing it
over anyway.
"""
import logging

import psycopg
import pytest

from shared.config import settings
from shared.errors import redact, safe_error
from tests._seed import TEAM_A


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def real_error(sql: str, params=None) -> psycopg.Error:
    """Make Postgres raise, and hand back what it raised.

    Constructed exceptions have no `diag`, and `diag` is where the safe half of
    a database error lives. A test built on a hand-made exception cannot tell
    whether the structural path works.
    """
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        try:
            conn.execute(sql, params)
        except psycopg.Error as exc:
            conn.rollback()
            return exc
    raise AssertionError("the statement was supposed to fail: " + sql)


#: A real one, produced by Postgres rather than invented. The parenthesised
#: tail is the entire row.
_PG_DETAIL = (
    'new row for relation "agent_runs" violates check constraint'
    ' "agent_runs_trigger_type_check"\n'
    'DETAIL:  Failing row contains (7214d057-f09e-402a-92f0-41f8cb681bdf,'
    ' aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa, mention, null, [], 0, running,'
    " 2026-09-08 02:23:14.604691+00, the staging password is hunter2).\n"
    "CONTEXT:  SQL statement \"insert into public.agent_runs ...\""
)


# ---------------------------------------------------------------------------
# The redactor
# ---------------------------------------------------------------------------

def test_a_failing_row_is_not_part_of_the_error():
    """🔴 It was, in full. `DETAIL: Failing row contains (...)` is every
    column of the row that failed, which for this system is a member's
    message, a document excerpt, or a prompt."""
    message = safe_error(psycopg.errors.CheckViolation(_PG_DETAIL))

    assert "hunter2" not in message
    assert "Failing row contains" not in message


def test_what_actually_broke_still_survives(seeded):
    """A redaction that removes the diagnosis is a worse bug than the leak.
    The constraint name is the thing an operator acts on, and Postgres reports
    it separately from the message — which is what makes it safe to keep."""
    exc = real_error(
        "insert into public.agent_runs (team_id, thread_id, requester_id,"
        " status, trigger_type) values (gen_random_uuid(), gen_random_uuid(),"
        " gen_random_uuid(), 'running', 'the staging password is hunter2')"
    )

    message = safe_error(exc)

    assert "agent_runs_trigger_type_check" in message
    assert "hunter2" not in message


def test_a_value_in_the_primary_message_does_not_survive_either(seeded):
    """🔴 Stripping DETAIL and CONTEXT was not enough. PostgreSQL puts values
    in the PRIMARY message too — `invalid input syntax for type uuid: "…"` —
    so a malformed identifier from a model, a document or a member landed
    intact in `jobs.last_error`, which the cross-team control role reads."""
    exc = real_error("select 1 from public.teams where id = %s",
                     ("PRIVATE_DOCUMENT_SENTINEL",))

    message = safe_error(exc)

    assert "PRIVATE_DOCUMENT_SENTINEL" not in message
    assert "22P02" in message, "the failure kind has to survive: " + message


def test_a_database_error_reports_no_message_text_at_all(seeded):
    """The rule, stated as a test: for database errors the output is built
    from the class, the SQLSTATE and Postgres' identifier fields. Nothing
    textual is carried across, because no pattern can be trusted to recognise
    a filename or a sentence."""
    exc = real_error("select 1 from public.teams where id = %s", ("not-a-uuid",))

    assert "invalid input syntax" not in safe_error(exc)


def test_a_connection_string_loses_its_password():
    """Any failure to connect quotes the DSN back, and after T26 there are
    five of them in this system."""
    exc = psycopg.OperationalError(
        "connection failed: postgresql://comrade_agent:s3cr3t@db:5432/postgres"
    )

    assert "s3cr3t" not in safe_error(exc)


@pytest.mark.parametrize("secret", [
    "ghp_0123456789abcdefghijklmnopqrstuvwxyz",   # GitHub token
    "ghs_0123456789abcdefghijklmnopqrstuvwxyz",   # installation token
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n-----END RSA PRIVATE KEY-----",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop",   # a JWT
])
def test_credentials_never_survive_an_error_message(secret):
    assert secret not in safe_error(RuntimeError(f"failed using {secret}"))


def test_an_ordinary_message_is_left_alone():
    """Most failures say something useful and short. Mangling those to defend
    against the ones that do not is how redaction gets turned off."""
    assert safe_error(ValueError("no handler registered for job_type: x")) == (
        "ValueError: no handler registered for job_type: x"
    )


def test_the_type_is_kept_even_when_the_message_is_dropped():
    """"Something went wrong" is not an error report. The class of failure is
    never the sensitive part and is most of the diagnosis."""
    message = safe_error(RuntimeError("ghp_0123456789abcdefghijklmnopqrstuvwxyz"))

    assert message.startswith("RuntimeError")


def test_a_long_error_is_bounded():
    """`last_error` is read by a screen and by an operator, and an unbounded
    one is a way to fill a table with a loop."""
    assert len(safe_error(RuntimeError("x" * 10_000))) <= 600


@pytest.mark.parametrize("message", [
    # Every one of these is a real string this system produces, gathered from
    # the paths that now pass through the redactor.
    "This turn used more than the team's remaining token budget for the hour"
    " (494,000), so Comrade stopped part-way.",
    "This team has used its 500,000 agent tokens for the hour (500,000 so far).",
    "no handler registered for job_type: parse_document",
    "this document has no stored file to read",
    "unsupported document type: .doc",
    "the container could not start: docker: Error response from daemon",
    "Docker is not available, and Comrade does not run a team's code outside"
    " a container. Start Docker and try again.",
    "no GitHub credential reaches acme/app.",
    "worker lease expired",
    "the clone failed",
])
def test_the_products_own_messages_pass_through_untouched(message):
    """The risk of a redactor is not that it misses something — it is that it
    mangles legitimate output and somebody turns it off. `job_type:` and
    `token budget` both sit close to patterns this matches, so this is the
    guard that keeps the rules from creeping wider than they should."""
    assert redact(message) == message


# ---------------------------------------------------------------------------
# Where it has to be applied
# ---------------------------------------------------------------------------

def test_a_failing_job_does_not_record_the_row_it_failed_on(seeded):
    """🔴 The end-to-end version. `comrade_control` can read
    `jobs.last_error` across every team — T26 took `payload` away from it and
    left this column carrying the same content."""
    from shared.db import Role, team_session
    from pipeline import worker

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        job_id = conn.execute(
            "insert into public.jobs (team_id, job_type, payload, dedupe_key)"
            " values (%s,'ingest_github','{}','redaction-1') returning id",
            (TEAM_A,),
        ).fetchone()[0]

    def _explode(team_id, payload):
        raise real_error(
            "insert into public.agent_runs (team_id, thread_id, requester_id,"
            " status, trigger_type) values (gen_random_uuid(),"
            " gen_random_uuid(), gen_random_uuid(), 'running',"
            " 'the staging password is hunter2')"
        )

    while worker.run_once(handlers={"ingest_github": _explode},
                          worker_id="redaction-test"):
        conn = _admin()
        try:
            recorded = conn.execute(
                "select last_error from public.jobs where id=%s", (job_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        if recorded:
            break

    assert recorded, "the failure was not recorded at all"
    assert "hunter2" not in recorded, recorded
    assert "agent_runs_trigger_type_check" in recorded


def test_a_failing_turn_does_not_record_the_row_it_failed_on(seeded, caplog):
    """The same column on `agent_runs`, which a member's screen renders."""
    from shared.agent_runs import finish_run, start_run

    conn = _admin()
    try:
        thread_id = conn.execute(
            "select id from public.threads where team_id=%s and title='General'",
            (TEAM_A,),
        ).fetchone()[0]
    finally:
        conn.close()

    from tests._seed import A1
    run_id = start_run(TEAM_A, A1, str(thread_id), None, "user", "a question")
    finish_run(TEAM_A, str(run_id), "failed", input_tokens=0, output_tokens=0,
               last_error=safe_error(psycopg.errors.CheckViolation(_PG_DETAIL)))

    conn = _admin()
    try:
        recorded = conn.execute(
            "select last_error from public.agent_runs where id=%s", (run_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert "hunter2" not in recorded


def test_the_log_does_not_carry_it_either(caplog):
    """A redacted column and an unredacted log line is not a redaction — the
    log is the copy that leaves the building."""
    logger = logging.getLogger("comrade.test.redaction")
    with caplog.at_level(logging.WARNING):
        logger.warning("job failed: %s",
                       safe_error(psycopg.errors.CheckViolation(_PG_DETAIL)))

    assert "hunter2" not in caplog.text


def test_our_own_exceptions_keep_the_sentence_somebody_wrote():
    """🔴 Prefixing the class name unconditionally put `PermanentJobError:` in
    front of messages written for MEMBERS. `jobs.last_error` is what the
    connect screen renders when a clone fails, so this would have shown a
    Python class name to somebody trying to work out why their repository did
    not appear."""
    from pipeline.worker import PermanentJobError
    from shared.usage import BudgetExceeded

    assert safe_error(PermanentJobError("unsupported input")) == "unsupported input"
    assert safe_error(BudgetExceeded("This team has used its turns.")) == (
        "This team has used its turns."
    )


def test_a_library_exception_still_keeps_its_class():
    """The distinction: nobody wrote a library exception's message for a
    person, and its type is most of the diagnosis."""
    assert safe_error(TimeoutError("took too long")).startswith("TimeoutError:")
