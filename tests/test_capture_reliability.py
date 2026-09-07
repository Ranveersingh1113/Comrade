"""What gets captured into memory, when, and what happens when it cannot be.

🔴 THE DEFECTS.

Capture fired on COUNT alone: five new group messages. A team that made one
important decision and then went quiet never reached the threshold, so the
decision was never captured — and "we decided X" is exactly the kind of thing
a team says once.

A batch was every message past the watermark, unbounded. A team coming back to
a fortnight of backlog produced one enormous transcript in one enormous model
call.

The watermark was a bare `created_at > since`. Two messages sharing a
timestamp at the boundary meant one was captured and the other skipped
FOREVER — the same defect T09 fixed in the message list, here in the path that
decides what the team remembers.

And `extract_candidates` ended `return list(parsed.facts) if parsed else []`.
`resp.parsed` is None when the model's answer could not be read at all, so a
malformed response was indistinguishable from "I read this and there was
nothing in it" — and the compile then wrote its row and ADVANCED the
watermark, discarding that stretch of conversation permanently.
"""
import datetime

import psycopg
import pytest

from shared.config import settings
from tests._seed import A1, A2, TEAM_A


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _thread(cur, team_id, title="General"):
    return cur.execute(
        "select id from public.threads where team_id=%s and title=%s",
        (team_id, title),
    ).fetchone()[0]


def _post(cur, team_id, sender, body, *, thread_id=None, created_at=None):
    thread_id = thread_id or _thread(cur, team_id)
    if created_at is None:
        return str(cur.execute(
            "insert into public.messages (team_id, thread_id, sender_kind,"
            " sender_id, body) values (%s,%s,'user',%s,%s) returning id",
            (team_id, thread_id, sender, body),
        ).fetchone()[0])
    return str(cur.execute(
        "insert into public.messages (team_id, thread_id, sender_kind,"
        " sender_id, body, created_at) values (%s,%s,'user',%s,%s,%s) returning id",
        (team_id, thread_id, sender, body, created_at),
    ).fetchone()[0])


def _clear(cur):
    cur.execute("delete from public.messages where team_id=%s", (TEAM_A,))
    cur.execute("delete from public.memory_compilations where team_id=%s", (TEAM_A,))
    cur.execute("delete from public.jobs where team_id=%s", (TEAM_A,))


# ---------------------------------------------------------------------------
# Trigger: count OR age
# ---------------------------------------------------------------------------

def test_one_quiet_decision_is_eventually_captured(seeded):
    """🔴 Five messages or nothing. A team that decided one thing and went
    quiet never reached the threshold, so the decision was never captured."""
    from pipeline.chat import enqueue_chat_compile

    conn = _admin()
    try:
        _clear(conn)
        old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
        _post(conn, TEAM_A, A1, "we are going with Postgres", created_at=old)
    finally:
        conn.close()

    assert enqueue_chat_compile(TEAM_A) is not None


def test_a_fresh_message_below_the_threshold_still_waits(seeded):
    """Age is a floor, not a bypass: a conversation still being typed should
    not be compiled a sentence at a time."""
    from pipeline.chat import enqueue_chat_compile

    conn = _admin()
    try:
        _clear(conn)
        _post(conn, TEAM_A, A1, "so I was thinking")
    finally:
        conn.close()

    assert enqueue_chat_compile(TEAM_A) is None


def test_the_sweep_finds_a_team_that_only_has_old_messages(seeded):
    """The prefilter has to agree with the authoritative check, or the sweep
    never calls it and the age rule is unreachable."""
    from pipeline.chat import sweep_chat_compiles

    conn = _admin()
    try:
        _clear(conn)
        old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
        _post(conn, TEAM_A, A1, "the deadline moved to Friday", created_at=old)
    finally:
        conn.close()

    assert sweep_chat_compiles() != []


# ---------------------------------------------------------------------------
# Bounded batches
# ---------------------------------------------------------------------------

def test_a_backlog_is_taken_in_batches_rather_than_all_at_once(seeded):
    """🔴 Unbounded. A team returning to a fortnight of backlog produced one
    enormous transcript in one enormous model call."""
    from pipeline.chat import MAX_CHAT_BATCH, enqueue_chat_compile

    conn = _admin()
    try:
        _clear(conn)
        old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
        for i in range(MAX_CHAT_BATCH + 7):
            _post(conn, TEAM_A, A1, f"message {i}",
                  created_at=old + datetime.timedelta(seconds=i))
        job_id = enqueue_chat_compile(TEAM_A)
        payload = conn.execute(
            "select payload from public.jobs where id=%s", (job_id,)
        ).fetchone()[0]
    finally:
        conn.close()

    assert len(payload["message_ids"]) == MAX_CHAT_BATCH


def test_a_batch_is_also_bounded_by_how_much_text_it_carries(seeded):
    """Record count alone does not bound a model call: fifty pasted stack
    traces is a different amount of work from fifty "ok"s."""
    from pipeline.chat import MAX_CHAT_BATCH_CHARS, enqueue_chat_compile

    conn = _admin()
    try:
        _clear(conn)
        old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
        big = "x" * (MAX_CHAT_BATCH_CHARS // 3)
        for i in range(6):
            _post(conn, TEAM_A, A1, big,
                  created_at=old + datetime.timedelta(seconds=i))
        job_id = enqueue_chat_compile(TEAM_A)
        payload = conn.execute(
            "select payload from public.jobs where id=%s", (job_id,)
        ).fetchone()[0]
    finally:
        conn.close()

    assert 0 < len(payload["message_ids"]) < 6


# ---------------------------------------------------------------------------
# Boundaries that do not lose records
# ---------------------------------------------------------------------------

def test_two_messages_sharing_a_timestamp_are_both_captured(seeded):
    """🔴 `created_at > since` skipped the second one forever. Same defect T09
    fixed in the message list, here in what the team remembers."""
    from pipeline.chat import fetch_new_chat_messages

    stamp = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    conn = _admin()
    try:
        _clear(conn)
        a = _post(conn, TEAM_A, A1, "half the decision", created_at=stamp)
        b = _post(conn, TEAM_A, A2, "the other half", created_at=stamp)
    finally:
        conn.close()
    # The batch is ordered by (created_at, id), so the boundary is whichever of
    # the tied pair sorts lower — ids are random, not insertion-ordered.
    boundary, neighbour = sorted([a, b])

    from shared.db import Role, team_session

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        remaining = fetch_new_chat_messages(conn, TEAM_A, stamp, since_id=boundary)

    assert [m["id"] for m in remaining] == [neighbour]


def test_a_message_still_being_committed_is_not_stepped_over(seeded):
    """A row committed after the watermark snapshot, but stamped before it,
    would sit below the watermark forever. The capture stays a little behind
    now() so in-flight writes have landed."""
    from pipeline.chat import CAPTURE_LAG_SECONDS, fetch_new_chat_messages
    from shared.db import Role, team_session

    conn = _admin()
    try:
        _clear(conn)
        _post(conn, TEAM_A, A1, "just this second")
    finally:
        conn.close()

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        captured = fetch_new_chat_messages(conn, TEAM_A, None)

    assert CAPTURE_LAG_SECONDS > 0
    assert captured == [], "a message written this instant is not settled yet"


# ---------------------------------------------------------------------------
# Per-thread context
# ---------------------------------------------------------------------------

def test_two_conversations_are_not_interleaved_into_one_transcript(seeded):
    """🔴 Every team-visible thread was ordered together by time, so two
    unrelated conversations arrived at the model as one exchange — and a model
    asked to extract decisions from that will happily invent the connection."""
    from pipeline.chat import format_transcript, group_by_thread

    # Grouped BEFORE numbering, which is the whole point: source_index has to
    # refer to the same order the model was shown, so one function orders and
    # the other numbers whatever it is given.
    lines = format_transcript(group_by_thread([
        {"id": "a", "sender": "Ann", "text": "ship on Friday", "thread_id": "t1",
         "thread_title": "Release"},
        {"id": "b", "sender": "Bo", "text": "the logo is too big", "thread_id": "t2",
         "thread_title": "Design"},
        {"id": "c", "sender": "Ann", "text": "agreed", "thread_id": "t1",
         "thread_title": "Release"},
    ]))

    # Same thread's lines adjacent, and each group says which thread it is.
    assert "Release" in lines and "Design" in lines
    assert lines.index("ship on Friday") < lines.index("agreed") < lines.index("the logo is too big")


# ---------------------------------------------------------------------------
# Malformed output is not an empty result
# ---------------------------------------------------------------------------

def test_an_unreadable_model_answer_is_a_failure_not_an_empty_result(monkeypatch):
    """🔴 `return list(parsed.facts) if parsed else []`. `resp.parsed` is None
    when the answer could not be read at all, so a malformed response looked
    exactly like "I read this and there was nothing in it"."""
    from types import SimpleNamespace

    from pipeline import compiler

    monkeypatch.setattr(compiler, "_get_client", lambda: SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda **_: SimpleNamespace(parsed=None),
        ),
    ))

    with pytest.raises(compiler.ExtractionUnavailable):
        compiler.extract_candidates("[0] Ann: ship on Friday", kind="chat")


def test_a_genuinely_empty_extraction_is_still_an_empty_result(monkeypatch):
    """Chitchat must remain compilable, or the watermark never advances."""
    from types import SimpleNamespace

    from pipeline import compiler

    monkeypatch.setattr(compiler, "_get_client", lambda: SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda **_: SimpleNamespace(
                parsed=SimpleNamespace(facts=[]),
            ),
        ),
    ))

    assert compiler.extract_candidates("[0] Ann: morning", kind="chat") == []


def test_a_failed_extraction_leaves_the_watermark_where_it_was(seeded, monkeypatch):
    """The point of the distinction: the batch must be retried, not lost."""
    from pipeline import chat, compiler
    from pipeline.chat import chat_watermark
    from shared.db import Role, team_session

    conn = _admin()
    try:
        _clear(conn)
        old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
        message_id = _post(conn, TEAM_A, A1, "we chose Postgres", created_at=old)
    finally:
        conn.close()

    def _boom(*_args, **_kwargs):
        raise compiler.ExtractionUnavailable("the model's answer could not be read")

    monkeypatch.setattr(chat, "extract_candidates", _boom)

    with pytest.raises(compiler.ExtractionUnavailable):
        chat.handle_chat_compile_job(
            TEAM_A, {"message_ids": [message_id], "through": old.isoformat()},
        )

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        assert chat_watermark(conn, TEAM_A) is None
