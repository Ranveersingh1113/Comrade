"""Telling a request to Comrade apart from a decision the team made.

🔴 THE DEFECT. The transcript handed to extraction was `[3] Name: message` and
nothing else. A line addressed to the agent — "@comrade investigate switching
to Postgres" — was indistinguishable from a line where the team settled
something — "we're switching to Postgres". So a request to look into an option
could be compiled into the wiki as a decision the team had taken, and the wiki
is what the agent reads back as fact on every later turn.

The reverse matters just as much: a decision announced TO Comrade is still a
decision. Provenance is context for the judgement, not a veto on it.

And the extraction metric measured recall alone. Its own docstring said so —
"If extras ever need judging, that is a precision metric and a different
labelling job." A prompt that extracts everything scores a perfect recall.
"""
import datetime

import psycopg

from shared.config import settings
from tests._seed import A1, A2, TEAM_A


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _clear(cur):
    cur.execute("delete from public.agent_runs where team_id=%s", (TEAM_A,))
    cur.execute("delete from public.messages where team_id=%s", (TEAM_A,))
    cur.execute("delete from public.memory_compilations where team_id=%s", (TEAM_A,))


def _post(cur, body, sender=A1, *, minutes_ago=60):
    thread_id = cur.execute(
        "select id from public.threads where team_id=%s and title='General'",
        (TEAM_A,),
    ).fetchone()[0]
    stamp = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(minutes=minutes_ago))
    return str(cur.execute(
        "insert into public.messages (team_id, thread_id, sender_kind, sender_id,"
        " body, created_at) values (%s,%s,'user',%s,%s,%s) returning id",
        (TEAM_A, thread_id, sender, body, stamp),
    ).fetchone()[0]), str(thread_id)


# ---------------------------------------------------------------------------
# Provenance reaches the model
# ---------------------------------------------------------------------------

def test_a_line_addressed_to_comrade_is_marked_as_such(seeded):
    """🔴 It was not, so a request to investigate an option and a decision to
    take it arrived at the model as the same kind of sentence."""
    from pipeline.chat import fetch_new_chat_messages, format_transcript
    from shared.db import Role, team_session

    conn = _admin()
    try:
        _clear(conn)
        asked, thread_id = _post(conn, "@comrade investigate switching to Postgres")
        conn.execute(
            "insert into public.agent_runs (team_id, requester_id, thread_id,"
            " input_message_id, trigger_type, input_summary, status)"
            " values (%s,%s,%s,%s,'user','investigate','done')",
            (TEAM_A, A1, thread_id, asked),
        )
        _post(conn, "we are switching to Postgres", sender=A2, minutes_ago=59)
    finally:
        conn.close()

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        messages = fetch_new_chat_messages(conn, TEAM_A, None)
    lines = format_transcript(messages).splitlines()

    directed = [line for line in lines if "investigate switching" in line]
    plain = [line for line in lines if "we are switching" in line]
    assert directed and "Comrade" in directed[0], directed
    assert plain and "Comrade" not in plain[0], plain


def test_a_message_that_only_mentions_comrade_is_marked_too(seeded):
    """Not every agent-directed message starts a run — a busy room refuses
    some, and a refused turn is still a question rather than a decision."""
    from pipeline.chat import fetch_new_chat_messages, format_transcript
    from shared.db import Role, team_session

    conn = _admin()
    try:
        _clear(conn)
        _post(conn, "@Comrade could you check the migration?")
    finally:
        conn.close()

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        messages = fetch_new_chat_messages(conn, TEAM_A, None)

    assert messages[0]["to_agent"] is True
    assert "Comrade" in format_transcript(messages).splitlines()[-1]


def test_an_ordinary_team_message_is_not_marked(seeded):
    from pipeline.chat import fetch_new_chat_messages
    from shared.db import Role, team_session

    conn = _admin()
    try:
        _clear(conn)
        _post(conn, "the deadline moved to Friday")
    finally:
        conn.close()

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        messages = fetch_new_chat_messages(conn, TEAM_A, None)

    assert messages[0]["to_agent"] is False


def test_a_line_a_member_asked_to_remember_says_so(seeded):
    """The codebase already calls this "the highest-signal fact in the
    system". It never reached the model."""
    from pipeline.chat import format_transcript

    lines = format_transcript(
        [{"id": "m", "sender": "Ann", "text": "we ship on Friday",
          "thread_id": "t", "thread_title": "Release", "to_agent": False}],
        trigger="on_demand",
    )

    assert "asked" in lines.lower() or "remember" in lines.lower()


# ---------------------------------------------------------------------------
# Precision, not only recall
# ---------------------------------------------------------------------------

def test_the_scorer_reports_a_false_positive_against_a_labelled_trap():
    """🔴 Recall alone. A prompt that extracts every sentence scores 1.0."""
    from evaluation.extraction import ExpectedFact, ForbiddenFact, score

    result = score(
        produced=[
            "The team decided to switch to Postgres",
            "The team is investigating a switch to MySQL",
        ],
        expected=[ExpectedFact("switching to Postgres", ("postgres",))],
        forbidden=[ForbiddenFact("investigating MySQL is not a decision", ("mysql",))],
    )

    assert result["recall"] == 1.0
    assert result["false_positives"] == ["The team is investigating a switch to MySQL"]
    assert result["precision"] == 0.5


def test_a_clean_run_scores_full_precision():
    from evaluation.extraction import ExpectedFact, ForbiddenFact, score

    result = score(
        produced=["The team decided to switch to Postgres"],
        expected=[ExpectedFact("switching to Postgres", ("postgres",))],
        forbidden=[ForbiddenFact("investigating MySQL", ("mysql",))],
    )

    assert result["recall"] == 1.0 and result["precision"] == 1.0
    assert result["false_positives"] == []


def test_an_unlabelled_extra_is_reported_but_not_punished():
    """The labeller lists what must be found and what must NOT be, not
    everything findable. Penalising every extra would punish thoroughness."""
    from evaluation.extraction import ExpectedFact, ForbiddenFact, score

    result = score(
        produced=[
            "The team decided to switch to Postgres",
            "Ann owns the migration",
        ],
        expected=[ExpectedFact("switching to Postgres", ("postgres",))],
        forbidden=[ForbiddenFact("investigating MySQL", ("mysql",))],
    )

    assert result["precision"] == 1.0
    assert "Ann owns the migration" in result["extra"]


def test_the_chat_set_labels_the_cases_the_plan_names():
    """Requests to investigate, tentative assignments, negation, corrections
    and quoted statements — each one a way a sentence can look like a decision
    without being one."""
    from evaluation.extraction_set import CHAT_SOURCES

    assert CHAT_SOURCES, "no chat fixtures"
    for source in CHAT_SOURCES:
        assert source.expected, f"{source.name} labels nothing to find"
        assert source.forbidden, f"{source.name} labels no traps"
