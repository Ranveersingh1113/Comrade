"""One agent turn at a time per room (findings §4.3).

Two simultaneous runs in one group room means two agents that cannot see each
other: duplicated work, contradictory answers, and a race on the consent queue.
It stops reading as one teammate.

Private threads still run in parallel — they share no surface, so serialising
them would only make members wait on each other for nothing.
"""
import psycopg
import pytest

from shared.config import settings
from shared.db import room_lock
from tests._seed import TEAM_A, TEAM_B


def test_a_second_holder_is_refused_while_the_first_holds(seeded):
    with room_lock(TEAM_A) as first:
        assert first is True
        with room_lock(TEAM_A) as second:
            assert second is False, "two turns took the same room's lock"


def test_the_lock_is_released_on_exit(seeded):
    with room_lock(TEAM_A) as first:
        assert first is True
    with room_lock(TEAM_A) as again:
        assert again is True, "the lock outlived its block"


def test_the_lock_is_released_even_when_the_turn_raises(seeded):
    """A turn that blows up must not wedge the room until a restart."""
    with pytest.raises(ValueError):
        with room_lock(TEAM_A) as held:
            assert held is True
            raise ValueError("turn exploded")

    with room_lock(TEAM_A) as again:
        assert again is True, "an exception leaked the lock"


def test_different_teams_do_not_block_each_other(seeded):
    with room_lock(TEAM_A) as a:
        assert a is True
        with room_lock(TEAM_B) as b:
            assert b is True, "one team's turn blocked another team's"


def test_the_lock_is_visible_in_pg_locks_while_held(seeded):
    """Proof it is a real Postgres advisory lock, not a Python flag."""
    conn = psycopg.connect(settings.comrade_db_url_admin)
    try:
        with room_lock(TEAM_A):
            held = conn.execute(
                "select count(*) from pg_locks where locktype='advisory'"
            ).fetchone()[0]
            assert held >= 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The runtime actually uses it
# ---------------------------------------------------------------------------

def test_a_group_turn_is_refused_while_the_room_is_held(seeded):
    """The behavioural half: holding the lock must stop a real group turn."""
    import asyncio

    from agent.runtime import stream_turn
    from tests._seed import A1

    async def _drain():
        return [
            f async for f in stream_turn(
                TEAM_A, A1, "status?", thread_type="group"
            )
        ]

    with room_lock(TEAM_A) as held:
        assert held is True
        frames = asyncio.run(_drain())

    assert [f["type"] for f in frames] == ["busy"]
    assert "someone else" in frames[0]["detail"]


def test_a_private_turn_ignores_the_room_lock(seeded, monkeypatch):
    """§4.3: private threads share no surface, so they never serialise."""
    import asyncio

    from agent.runtime import stream_turn
    from tests._seed import A1

    async def _fake_run(*_a, **_k):
        return
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr("agent.runtime.Runner.run_async", _fake_run)

    async def _drain():
        return [
            f async for f in stream_turn(
                TEAM_A, A1, "status?", thread_type="private"
            )
        ]

    with room_lock(TEAM_A) as held:
        assert held is True
        frames = asyncio.run(_drain())

    assert "busy" not in [f.get("type") for f in frames]
    assert frames[0]["type"] == "run"
