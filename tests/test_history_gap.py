"""The messages between the summary and the replay window.

🔴 THE DEFECT (fix.md F16). Two windows that were never made to meet.

The summary covers an old prefix. Compaction only runs once there are
`MIN_COMPACT_MESSAGES` (40) messages older than the `KEEP_RECENT_MESSAGES`
(20) it refuses to touch — so the summary advances in jumps of 40. The runtime
replays the last `agent_history_turns` (20) messages.

Between those, up to 39 messages are in neither. With a summary through
message 40, a turn at message 61 replays 42–61 and the summary covers 1–40:
message 41 is invisible. By message 99 the hole is 41–79.

T19 exists because "a constraint stated a hundred messages ago was invisible
to every later turn". It fixed the far end and left a moving hole just behind
the replay window, which is where a constraint stated ten minutes ago lives.
"""
import datetime
import uuid

import psycopg
import pytest

from agent.history import KEEP_RECENT_MESSAGES, replay_for_turn, set_summary
from shared.config import settings
from tests._seed import A1, TEAM_A


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _thread() -> str:
    conn = _admin()
    try:
        return str(conn.execute(
            "select id from public.threads where team_id=%s and title='General'",
            (TEAM_A,),
        ).fetchone()[0])
    finally:
        conn.close()


def _say(thread_id: str, n: int, *, tied: bool = False) -> list[tuple[str, str]]:
    """`n` messages, oldest first. Returns (id, body) in order."""
    conn = _admin()
    try:
        base = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
        out = []
        for i in range(n):
            message_id = str(uuid.UUID(int=(1 << 100) + i))
            when = base if tied else base + datetime.timedelta(seconds=i)
            body = f"message {i + 1}"
            conn.execute(
                "insert into public.messages (team_id, thread_id, id,"
                " sender_kind, sender_id, body, created_at) values"
                " (%s,%s,%s,'user',%s,%s,%s)",
                (TEAM_A, thread_id, message_id, A1, body, when),
            )
            out.append((message_id, body))
        return out
    finally:
        conn.close()


def _replayed(thread_id: str) -> list[str]:
    """The message bodies this turn would actually see."""
    # The composition the runtime uses, not a reconstruction of it: two
    # callers assembling this separately is how the summary and the replay
    # came to disagree in the first place.
    turns = replay_for_turn(
        TEAM_A, A1, thread_id, settings.agent_history_turns, None,
    )
    texts = []
    for content in turns:
        for part in content.parts:
            if getattr(part, "text", None):
                texts.append(part.text)
    return texts


@pytest.fixture
def thread(seeded):
    thread_id = _thread()
    conn = _admin()
    try:
        conn.execute("delete from public.messages where thread_id=%s", (thread_id,))
        conn.execute("delete from public.thread_working_state where thread_id=%s",
                     (thread_id,))
    finally:
        conn.close()
    return thread_id


# ---------------------------------------------------------------------------

def test_the_message_after_the_summary_is_still_visible(thread):
    """🔴 The finding's own case. A summary through message 40 and a thread
    at 61: message 41 was in neither the summary nor the replay."""
    messages = _say(thread, 61)
    through_id, _ = messages[39]                      # message 40
    conn = _admin()
    try:
        when = conn.execute(
            "select created_at from public.messages where id=%s", (through_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    set_summary(TEAM_A, thread, "the team agreed some things",
                through=when, through_id=through_id)

    seen = "\n".join(_replayed(thread))

    assert "message 41" in seen, (
        "the message right after the summary fell between the two windows"
    )


def test_every_unsummarised_message_is_visible(thread):
    """The whole tail, not just its far end — the hole grows to 39 messages
    before the next compaction closes it."""
    messages = _say(thread, 61)
    through_id, _ = messages[39]
    conn = _admin()
    try:
        when = conn.execute(
            "select created_at from public.messages where id=%s", (through_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    set_summary(TEAM_A, thread, "summary", through=when, through_id=through_id)

    seen = "\n".join(_replayed(thread))

    missing = [f"message {i}" for i in range(41, 62)
               if f"message {i}" not in seen]
    assert not missing, f"invisible to this turn: {missing}"


def test_nothing_is_replayed_twice(thread):
    """The other half of the contract. The summary already represents 1–40;
    replaying them as messages too would duplicate the conversation."""
    messages = _say(thread, 61)
    through_id, _ = messages[39]
    conn = _admin()
    try:
        when = conn.execute(
            "select created_at from public.messages where id=%s", (through_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    set_summary(TEAM_A, thread, "summary", through=when, through_id=through_id)

    seen = _replayed(thread)

    assert not [t for t in seen if "message 40" in t], (
        "a message the summary already covers was replayed as well"
    )


def test_a_thread_with_no_summary_keeps_the_recent_window(thread):
    """Unchanged behaviour where there is nothing to bridge from."""
    _say(thread, 40)

    seen = _replayed(thread)

    assert len(seen) == settings.agent_history_turns


def test_a_short_thread_is_shown_whole(thread):
    _say(thread, 5)

    assert len(_replayed(thread)) == 5


def test_tied_timestamps_do_not_break_the_boundary(thread):
    """The cursor is compound because timestamps are not unique. A summary
    ending inside a tied group must resume after the right message, not after
    all of them or none."""
    messages = _say(thread, 61, tied=True)
    through_id, _ = messages[39]
    conn = _admin()
    try:
        when = conn.execute(
            "select created_at from public.messages where id=%s", (through_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    set_summary(TEAM_A, thread, "summary", through=when, through_id=through_id)

    seen = "\n".join(_replayed(thread))

    assert "message 41" in seen
    assert "message 40" not in seen


def test_the_tail_is_bounded_when_compaction_falls_behind(thread):
    """A tail that grows without limit is an unbounded prompt. If compaction
    stops running, the context has to stay finite — and that is a different
    failure from the gap, so it is capped rather than left to grow."""
    _say(thread, 300)

    seen = _replayed(thread)

    assert len(seen) <= 200, f"replayed {len(seen)} messages with no summary"
    assert len(seen) >= KEEP_RECENT_MESSAGES
