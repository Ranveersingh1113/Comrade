"""Messages that arrived in the same instant.

🔴 THE DEFECT (fix.md F15). Capture resumes from a COMPOUND cursor —
`chat_keyset` returns `(chat_through, chat_through_id)` and the bounded fetch
uses both, because timestamps are not unique and a batch boundary can fall in
the middle of a group that shares one.

The candidate sweep compared timestamps only: `m.created_at >
max(chat_through)`. That excludes every row AT the boundary timestamp —
including the ones after the boundary id that the fetch would happily take —
so the sweep never asked for another job. Those messages waited for unrelated
later chatter to arrive, and on a quiet team that is forever.

Two definitions of "what is left" in one system, and the one that decides
whether to look was the coarser of the two.
"""
import datetime
import uuid

import psycopg
import pytest

from pipeline import chat
from shared.config import settings
from tests._seed import A1, TEAM_A

#: More than one batch (MAX_CHAT_BATCH is 60), all at the same instant.
TIED = 61


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


@pytest.fixture
def tied(seeded):
    """`TIED` messages sharing one `created_at`, with ordered ids."""
    conn = _admin()
    try:
        conn.execute("delete from public.memory_compilations where team_id=%s",
                     (TEAM_A,))
        conn.execute(
            "delete from public.messages where team_id=%s", (TEAM_A,))
        thread_id = conn.execute(
            "select id from public.threads where team_id=%s and title='General'",
            (TEAM_A,),
        ).fetchone()[0]
        when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
        ids = []
        for i in range(TIED):
            # Ids ascend with the sequence, so the compound cursor has a
            # stable order to resume from within the tie.
            message_id = uuid.UUID(int=(1 << 100) + i)
            conn.execute(
                "insert into public.messages (team_id, thread_id, id,"
                " sender_kind, sender_id, body, created_at) values"
                " (%s,%s,%s,'user',%s,%s,%s)",
                (TEAM_A, thread_id, str(message_id), A1, f"line {i}", when),
            )
            ids.append(str(message_id))
    finally:
        conn.close()
    return ids, when


def _capture_through(ids: list[str], when, upto: int) -> None:
    """Record a completed compilation that stopped inside the tie."""
    conn = _admin()
    try:
        conn.execute(
            "insert into public.memory_compilations (team_id, status, trigger,"
            " chat_through, chat_through_id) values (%s,'done','scheduled',%s,%s)",
            (TEAM_A, when, ids[upto]),
        )
    finally:
        conn.close()


def _would_sweep() -> bool:
    """Whether the sweep still considers this team to have work."""
    return TEAM_A in [str(t) for t in chat.teams_with_uncaptured_chat()] \
        if hasattr(chat, "teams_with_uncaptured_chat") else _sweep_finds_team()


def _sweep_finds_team() -> bool:
    """The sweep's own candidate query, asked directly."""
    from shared.db import Role, connect

    with connect(Role.CONTROL) as conn:
        conn.autocommit = True
        rows = conn.execute(
            "select 1 from public.messages m"
            " join public.threads th on th.id=m.thread_id and th.team_id=m.team_id"
            " where m.team_id=%s and" + chat.CAPTURABLE_SQL +
            "   and" + chat.UNCAPTURED_SQL.format(team="m.team_id") +
            " limit 1",
            (TEAM_A,),
        ).fetchall()
    return bool(rows)


# ---------------------------------------------------------------------------

def test_the_fixture_really_ties(tied):
    """Without this the tests below could pass on distinct timestamps."""
    ids, _ = tied
    conn = _admin()
    try:
        distinct = conn.execute(
            "select count(distinct created_at) from public.messages"
            " where team_id=%s", (TEAM_A,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert distinct == 1
    assert len(ids) == TIED


def test_a_boundary_inside_the_tie_leaves_work_the_sweep_can_see(tied):
    """🔴 The sweep said no. Capture stopped in the middle of the tied group,
    the remaining rows share the boundary timestamp, and `created_at >` is
    false for every one of them — so nothing ever asked for the next batch."""
    ids, when = tied
    _capture_through(ids, when, upto=len(ids) // 2)

    assert _sweep_finds_team(), (
        "capture stopped mid-tie and the sweep reported nothing left to do"
    )


def test_capturing_the_whole_tie_leaves_nothing(tied):
    """The other end: once the last of the tied group is taken, the sweep has
    to go quiet, or it spins forever on work that is done."""
    ids, when = tied
    _capture_through(ids, when, upto=len(ids) - 1)

    assert not _sweep_finds_team()


def test_the_fetch_and_the_sweep_agree_about_what_is_left(tied):
    """The property underneath both: one definition of "not captured yet",
    used by the thing that decides to look and the thing that reads."""
    ids, when = tied
    _capture_through(ids, when, upto=len(ids) // 2)

    from shared.db import Role, team_session

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        through, through_id = chat.chat_keyset(conn, TEAM_A)
        remaining = conn.execute(
            "select count(*) from public.messages m"
            " join public.threads th on th.id=m.thread_id"
            "  and th.team_id=m.team_id"
            " where m.team_id=%s and" + chat.CAPTURABLE_SQL +
            "   and (m.created_at, m.id) > (%s, %s::uuid)",
            (TEAM_A, through, through_id),
        ).fetchone()[0]

    assert remaining == TIED - (len(ids) // 2) - 1
    assert _sweep_finds_team() is (remaining > 0)


def test_an_unfinished_compilation_does_not_move_the_cursor(tied):
    """A compile that is still running has captured nothing, so the sweep has
    to keep seeing the work."""
    ids, when = tied
    conn = _admin()
    try:
        conn.execute(
            "insert into public.memory_compilations (team_id, status, trigger,"
            " chat_through, chat_through_id) values"
            " (%s,'running','scheduled',%s,%s)", (TEAM_A, when, ids[-1]),
        )
    finally:
        conn.close()

    assert _sweep_finds_team()
