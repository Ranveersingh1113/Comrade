"""The hole before the first summary exists.

🔴 THE DEFECT (fix.md F45). My own F16 fix, tested only on the case it named.

F16 was that the summary covered an old prefix while the replay showed the last
`agent_history_turns` (20), and nothing made the two meet. The fix takes the
summary's cursor and replays everything after it. It is bounded by
MAX_REPLAY_MESSAGES so a stalled compaction cannot become an unbounded prompt.

But the expanded replay is conditional:

    bound = MAX_REPLAY_MESSAGES if through is not None else limit

`through` comes from `summary_through`, which does not exist until the first
compaction has run — and compaction waits for MIN_COMPACT_MESSAGES (40) older
than the KEEP_RECENT_MESSAGES (20) it will not touch, so the first summary
arrives at message 60. Before that, a thread replays its last 20 messages and
nothing else. A constraint stated in message 1 is invisible from message 21
onwards, for forty messages, on every new thread there has ever been.

WHY MY OWN TEST MISSED IT. `test_the_tail_is_bounded_when_compaction_falls_behind`
put 300 messages in a thread with no summary and asserted the replay was
`<= 200` and `>= KEEP_RECENT_MESSAGES`. It got 20 — which satisfies both — so
the assertion was loose enough to pass on exactly the broken value. The
finding's own instruction is the fix for that too: "Test an ordinary fresh
thread, not only one preseeded with a summary cursor."
"""
import datetime
import uuid

import psycopg
import pytest

from agent.history import (
    KEEP_RECENT_MESSAGES, MAX_REPLAY_MESSAGES, replay_for_turn,
)
from shared.config import settings
from tests._seed import A1, TEAM_A

#: What compaction waits for before the first summary exists.
FIRST_SUMMARY_AT = 60


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


@pytest.fixture
def thread(seeded):
    conn = _admin()
    try:
        thread_id = str(conn.execute(
            "select id from public.threads where team_id=%s and title='General'",
            (TEAM_A,),
        ).fetchone()[0])
        conn.execute("delete from public.messages where thread_id=%s", (thread_id,))
        conn.execute("delete from public.thread_working_state where thread_id=%s",
                     (thread_id,))
    finally:
        conn.close()
    return thread_id


def _say(thread_id: str, n: int) -> None:
    """`n` messages, oldest first, on a thread nobody has compacted."""
    conn = _admin()
    try:
        base = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
        for i in range(n):
            conn.execute(
                "insert into public.messages (team_id, thread_id, id,"
                " sender_kind, sender_id, body, created_at) values"
                " (%s,%s,%s,'user',%s,%s,%s)",
                (TEAM_A, thread_id, str(uuid.UUID(int=(1 << 100) + i)), A1,
                 f"message {i + 1}", base + datetime.timedelta(seconds=i)),
            )
    finally:
        conn.close()


def _replayed(thread_id: str) -> list[str]:
    texts: list[str] = []
    for content in replay_for_turn(
        TEAM_A, A1, thread_id, settings.agent_history_turns, None,
    ):
        for part in content.parts:
            if getattr(part, "text", None):
                texts.append(part.text)
    return texts


# ---------------------------------------------------------------------------

def test_the_first_message_survives_to_the_first_summary(thread):
    """🔴 The finding's own case. A fresh thread, no summary anywhere, and a
    constraint stated at the start has to still be visible at message 59 —
    because nothing has summarised it yet and nothing will until 60."""
    _say(thread, FIRST_SUMMARY_AT - 1)

    seen = "\n".join(_replayed(thread))

    assert "message 1" in seen, (
        "the opening message vanished before anything had summarised it"
    )


@pytest.mark.parametrize("total", [21, 30, 45, 59])
def test_no_message_is_lost_anywhere_before_the_first_compaction(thread, total):
    """The whole span the old bound dropped: 21 through 59. Parameterised
    because the defect is a RANGE, and a single length would have let the
    boundary move without anybody noticing."""
    _say(thread, total)

    # Exact, not substring: "message 4" is inside "message 40", so a joined
    # blob reports 4 and 5 as present on any thread longer than 40 whether they
    # are or not. That flaw would hide a genuinely missing message, so the
    # check is against whole bodies.
    seen = {text.split(": ", 1)[-1] for text in _replayed(thread)}

    missing = [f"message {i}" for i in range(1, total + 1)
               if f"message {i}" not in seen]
    assert not missing, f"invisible on a thread of {total}: {missing}"


def test_a_thread_past_the_first_compaction_point_is_still_bounded(thread):
    """The other half of F16's reasoning has to survive: an unsummarised tail
    that grows without limit is an unbounded prompt on every turn. The cap
    applies whether or not a summary exists."""
    _say(thread, MAX_REPLAY_MESSAGES + 80)

    seen = _replayed(thread)

    assert len(seen) <= MAX_REPLAY_MESSAGES
    # And it is the RECENT end that is kept, not the ancient one.
    assert f"message {MAX_REPLAY_MESSAGES + 80}" in "\n".join(seen)


def test_the_bound_is_not_the_turn_limit_on_a_fresh_thread(thread):
    """The assertion my own test should have made. `agent_history_turns` is 20
    and the tail here is 40, so a replay of exactly 20 is the defect —
    "<= 200 and >= 20" passed on it."""
    _say(thread, 40)

    seen = _replayed(thread)

    assert len(seen) == 40, (
        f"replayed {len(seen)} of 40 unsummarised messages"
    )
    assert len(seen) != settings.agent_history_turns


def test_a_short_thread_is_still_shown_whole(thread):
    _say(thread, 5)

    assert len(_replayed(thread)) == 5


def test_an_empty_thread_replays_nothing(thread):
    assert _replayed(thread) == []
