"""Conversation history: the agent remembers the thread it is standing in.

History comes from `messages` (the documented source of truth), read as the
requesting member, and is scoped to one canonical thread UUID. RLS authorizes
the visible set; this module selects exactly one member-visible thread from it.
"""
import asyncio

import psycopg
import pytest

from agent.agent import APP_NAME
from agent.history import recent_turns
from agent.runtime import stream_turn
from shared.config import settings
from tests._seed import A1, A2, B1, TEAM_A, TEAM_B, as_user

GROUP_MARK = "zebra-group-marker-9f2"
PRIVATE_MARK = "walrus-private-marker-4c7"


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _msg(conn, team_id, thread_type, owner, kind, sender, body, order=0):
    """Insert one message at now()+order seconds.

    Explicit ordering because created_at defaults to now(), which is the
    TRANSACTION timestamp -- rows written together would otherwise tie. The
    offset also puts these rows after the ones tests/_seed.py wrote.
    """
    # thread_type/thread_owner_id were dropped by the contract migration
    # (20260904100000). The signature keeps naming a thread the way these
    # tests think about it — "the group room", "A1's private thread" — and
    # resolves it to the canonical id here, so no caller had to change.
    row = conn.execute(
        "insert into public.messages (team_id, thread_id,"
        " sender_kind, sender_id, body, created_at)"
        " values (%s,%s,%s,%s,%s, now() + make_interval(secs => %s))"
        " returning id",
        (team_id, _thread_id(team_id, thread_type, owner), kind, sender, body, order),
    ).fetchone()
    return str(row[0])


def _thread_id(team_id, thread_type, owner=None) -> str:
    conn = _admin()
    try:
        if thread_type == "group":
            row = conn.execute(
                "select id from public.threads where team_id=%s"
                " and visibility='team' and title='General'",
                (team_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "select id from public.threads where team_id=%s"
                " and owner_id=%s",
                (team_id, owner),
            ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0])


def _texts(contents):
    return [p.text for c in contents for p in c.parts]


async def _drain(agen):
    return [item async for item in agen]


# ---------- shape ----------

def test_prior_turns_come_back_oldest_first_with_adk_roles(seeded):
    conn = _admin()
    try:
        _msg(conn, TEAM_A, "private", A1, "user", A1, "when is the demo?", 1)
        _msg(conn, TEAM_A, "private", A1, "ai", None, "Friday at noon.", 2)
    finally:
        conn.close()

    turns = recent_turns(TEAM_A, A1, _thread_id(TEAM_A, "private", A1), 10)

    assert [c.role for c in turns][-2:] == ["user", "model"]
    assert _texts(turns)[-2:] == ["when is the demo?", "Friday at noon."]


def test_history_is_capped_to_the_most_recent_turns(seeded):
    conn = _admin()
    try:
        for i in range(6):
            _msg(conn, TEAM_A, "private", A1, "user", A1, f"line {i}", i + 1)
    finally:
        conn.close()

    assert _texts(recent_turns(
        TEAM_A, A1, _thread_id(TEAM_A, "private", A1), 2
    )) == ["line 4", "line 5"]


def test_zero_limit_reads_nothing(seeded):
    assert recent_turns(TEAM_A, A1, _thread_id(TEAM_A, "private", A1), 0) == []


# ---------- the boundary that matters ----------

def test_member_can_read_both_threads_under_rls(seeded):
    """Non-vacuity guard: RLS is NOT what separates group from private here."""
    conn = _admin()
    try:
        _msg(conn, TEAM_A, "group", None, "user", A2, GROUP_MARK, 1)
        _msg(conn, TEAM_A, "private", A1, "user", A1, PRIVATE_MARK, 2)
    finally:
        conn.close()

    with as_user(A1) as conn:
        bodies = [
            r[0] for r in conn.execute(
                "select body from public.messages where team_id=%s", (TEAM_A,)
            ).fetchall()
        ]
    assert GROUP_MARK in bodies and PRIVATE_MARK in bodies


def test_private_history_does_not_pull_in_the_group_room(seeded):
    conn = _admin()
    try:
        _msg(conn, TEAM_A, "group", None, "user", A2, GROUP_MARK, 1)
        _msg(conn, TEAM_A, "private", A1, "user", A1, PRIVATE_MARK, 2)
    finally:
        conn.close()

    texts = " ".join(_texts(recent_turns(
        TEAM_A, A1, _thread_id(TEAM_A, "private", A1), 50
    )))
    assert PRIVATE_MARK in texts
    assert GROUP_MARK not in texts


def test_group_history_does_not_pull_in_a_private_thread(seeded):
    conn = _admin()
    try:
        _msg(conn, TEAM_A, "group", None, "user", A2, GROUP_MARK, 1)
        _msg(conn, TEAM_A, "private", A1, "user", A1, PRIVATE_MARK, 2)
    finally:
        conn.close()

    texts = " ".join(_texts(recent_turns(
        TEAM_A, A1, _thread_id(TEAM_A, "group"), 50
    )))
    assert GROUP_MARK in texts
    assert PRIVATE_MARK not in texts


def test_history_does_not_mix_two_visible_team_threads(seeded):
    """RLS allows both public threads; the exact UUID selects only one."""
    conn = _admin()
    try:
        other = conn.execute(
            "insert into public.threads (team_id, title, visibility, kind, created_by)"
            " values (%s,'Release work','team','discussion',%s) returning id",
            (TEAM_A, A1),
        ).fetchone()[0]
        conn.execute(
            "insert into public.messages (team_id, thread_id, sender_kind,"
            " sender_id, body) values (%s,%s,'user',%s,'other-thread-marker')",
            (TEAM_A, other, A2),
        )
    finally:
        conn.close()

    general = _thread_id(TEAM_A, "group")
    assert "other-thread-marker" not in " ".join(_texts(recent_turns(
        TEAM_A, A1, general, 50
    )))
    assert "other-thread-marker" in " ".join(_texts(recent_turns(
        TEAM_A, A1, str(other), 50
    )))


def test_another_members_private_thread_never_appears(seeded):
    conn = _admin()
    try:
        # A2's personal thread has to be built here. The contract migration
        # backfilled one restricted thread per (team, owner) FOUND IN
        # MESSAGES, and the seed only writes a private message for A1 — so A2
        # has no personal thread until someone gives them one. Created
        # explicitly rather than teaching _thread_id to get-or-create: a
        # helper that invents a thread when it cannot find one would hide the
        # absence from every other test that calls it.
        conn.execute(
            "insert into public.threads"
            " (team_id, title, visibility, kind, created_by, owner_id)"
            " values (%s,'Private','restricted','discussion',%s,%s)",
            (TEAM_A, A2, A2),
        )
        _msg(conn, TEAM_A, "private", A2, "user", A2, "a2-only-secret", 1)
    finally:
        conn.close()

    assert "a2-only-secret" not in " ".join(
        _texts(recent_turns(TEAM_A, A1, _thread_id(TEAM_A, "private", A1), 50))
    )
    assert "a2-only-secret" not in " ".join(
        _texts(recent_turns(TEAM_A, A2, _thread_id(TEAM_A, "group"), 50))
    )


def test_history_never_crosses_teams(seeded):
    """Non-vacuous: A1 is a member of BOTH teams here, so `authenticated` can
    read team B's room. Only the explicit team_id filter keeps it out."""
    conn = _admin()
    try:
        conn.execute(
            "insert into public.memberships (team_id, user_id, role, status)"
            " values (%s,%s,'member','active')",
            (TEAM_B, A1),
        )
        _msg(conn, TEAM_B, "group", None, "user", B1, "team-b-only-marker", 1)
    finally:
        conn.close()

    with as_user(A1) as conn:
        assert conn.execute(
            "select count(*) from public.messages where team_id=%s", (TEAM_B,)
        ).fetchone()[0] > 0

    assert "team-b-only-marker" not in " ".join(
        _texts(recent_turns(TEAM_A, A1, _thread_id(TEAM_A, "group"), 50))
    )


# ---------- the two exclusions ----------

def test_the_message_this_turn_is_about_is_not_replayed(seeded):
    """server/app.py persists the member's message BEFORE the runtime runs, so
    a naive 'last N' would send it to the model twice."""
    conn = _admin()
    try:
        _msg(conn, TEAM_A, "private", A1, "user", A1, "earlier question", 1)
        current = _msg(conn, TEAM_A, "private", A1, "user", A1,
                       "the new question", 2)
    finally:
        conn.close()

    texts = _texts(recent_turns(TEAM_A, A1, _thread_id(TEAM_A, "private", A1), 50,
                                exclude_message_id=current))
    assert "earlier question" in texts
    assert "the new question" not in texts


@pytest.mark.parametrize("scope", ["everyone", "me"])
def test_a_tombstoned_message_does_not_re_enter_context(seeded, scope):
    conn = _admin()
    try:
        mid = _msg(conn, TEAM_A, "private", A1, "user", A1, "please forget this", 1)
        conn.execute(
            "update public.messages set deleted_scope=%s, deleted_by=%s,"
            " deleted_at=now() where id=%s",
            (scope, A1, mid),
        )
    finally:
        conn.close()

    assert "please forget this" not in " ".join(
        _texts(recent_turns(TEAM_A, A1, _thread_id(TEAM_A, "private", A1), 50))
    )


# ---------- wiring into the ADK session ----------

class _FakeRunner:
    """Stands in for ADK's Runner: records what the runtime handed it, and the
    session it was told to run against."""

    last: "_FakeRunner"

    def __init__(self, *, app, session_service):
        self.session_service = session_service
        self.kwargs = None
        self.session = None
        _FakeRunner.last = self

    async def run_async(self, **kwargs):
        self.kwargs = kwargs
        self.session = await self.session_service.get_session(
            app_name=APP_NAME,
            user_id=kwargs["user_id"],
            session_id=kwargs["session_id"],
        )
        return
        yield  # pragma: no cover - only here to make this an async generator


@pytest.fixture
def fake_runner(monkeypatch):
    monkeypatch.setattr("agent.runtime.Runner", _FakeRunner)
    return _FakeRunner


def test_stream_turn_seeds_the_session_with_the_thread_history(seeded, fake_runner):
    conn = _admin()
    try:
        _msg(conn, TEAM_A, "private", A1, "user", A1, "earlier question", 1)
        _msg(conn, TEAM_A, "private", A1, "ai", None, "earlier answer", 2)
    finally:
        conn.close()

    asyncio.run(_drain(stream_turn(
        TEAM_A, A1, "follow-up", thread_id=_thread_id(TEAM_A, "private", A1)
    )))

    events = fake_runner.last.session.events
    assert [e.content.role for e in events][-2:] == ["user", "model"]
    assert [e.author for e in events][-2:] == ["user", "comrade"]
    assert [e.content.parts[0].text for e in events][-2:] == [
        "earlier question", "earlier answer"
    ]


def test_stream_turn_caps_the_llm_calls(seeded, fake_runner):
    asyncio.run(_drain(stream_turn(
        TEAM_A, A1, "hi", thread_id=_thread_id(TEAM_A, "private", A1)
    )))
    assert settings.agent_max_llm_calls > 0
    assert (
        fake_runner.last.kwargs["run_config"].max_llm_calls
        == settings.agent_max_llm_calls
    )


def test_stream_turn_uses_the_thread_it_was_given(seeded, fake_runner):
    """A group turn seeds the room, not the caller's private thread."""
    conn = _admin()
    try:
        _msg(conn, TEAM_A, "group", None, "user", A2, GROUP_MARK, 1)
        _msg(conn, TEAM_A, "private", A1, "user", A1, PRIVATE_MARK, 2)
    finally:
        conn.close()

    asyncio.run(_drain(stream_turn(
        TEAM_A, A1, "hi", thread_id=_thread_id(TEAM_A, "group")
    )))
    texts = " ".join(
        e.content.parts[0].text for e in fake_runner.last.session.events
    )
    assert GROUP_MARK in texts and PRIVATE_MARK not in texts


def test_stream_turn_skips_the_message_it_was_handed(seeded, fake_runner):
    conn = _admin()
    try:
        current = _msg(conn, TEAM_A, "private", A1, "user", A1,
                       "the new question", 1)
    finally:
        conn.close()

    asyncio.run(_drain(stream_turn(
        TEAM_A, A1, "the new question", thread_id=_thread_id(TEAM_A, "private", A1),
        exclude_message_id=current,
    )))
    texts = [e.content.parts[0].text for e in fake_runner.last.session.events]
    assert "the new question" not in texts
