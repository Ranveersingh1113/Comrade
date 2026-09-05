"""One agent turn at a time per canonical thread."""
import asyncio
from contextlib import contextmanager

import psycopg
import pytest
from psycopg_pool import PoolTimeout

from agent.runtime import stream_turn
from shared.config import settings
from shared.db import thread_lock
from tests._seed import A1, TEAM_A, TEAM_B


def _thread_id(team_id: str, *, private: bool = False) -> str:
    conn = psycopg.connect(settings.comrade_db_url_admin)
    try:
        if private:
            row = conn.execute(
                "select id from public.threads where team_id=%s"
                " and owner_id=%s",
                (team_id, A1),
            ).fetchone()
        else:
            row = conn.execute(
                "select id from public.threads where team_id=%s and title='General'",
                (team_id,),
            ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0])


def test_a_second_holder_is_refused_while_the_first_holds(seeded):
    thread_id = _thread_id(TEAM_A)
    with thread_lock(thread_id) as first:
        assert first is True
        with thread_lock(thread_id) as second:
            assert second is False, "two turns took the same thread lock"


def test_the_lock_is_released_on_exit_and_exception(seeded):
    thread_id = _thread_id(TEAM_A)
    with pytest.raises(ValueError):
        with thread_lock(thread_id) as held:
            assert held is True
            raise ValueError("turn exploded")
    with thread_lock(thread_id) as again:
        assert again is True


def test_an_exhausted_lock_pool_refuses_without_waiting(monkeypatch):
    class ExhaustedPool:
        @contextmanager
        def connection(self, *, timeout):
            assert timeout == 0.01
            raise PoolTimeout()
            yield  # pragma: no cover - makes this a generator context manager

    monkeypatch.setattr("shared.db._advisory_lock_pool", ExhaustedPool)

    with thread_lock("not-a-real-thread") as held:
        assert held is False


def test_different_threads_in_one_team_do_not_block_each_other(seeded):
    general = _thread_id(TEAM_A)
    private = _thread_id(TEAM_A, private=True)
    with thread_lock(general) as first:
        assert first is True
        with thread_lock(private) as second:
            assert second is True


def test_different_teams_do_not_block_each_other(seeded):
    with thread_lock(_thread_id(TEAM_A)) as a:
        assert a is True
        with thread_lock(_thread_id(TEAM_B)) as b:
            assert b is True


def test_the_lock_is_visible_in_pg_locks_while_held(seeded):
    conn = psycopg.connect(settings.comrade_db_url_admin)
    try:
        with thread_lock(_thread_id(TEAM_A)):
            held = conn.execute(
                "select count(*) from pg_locks where locktype='advisory'"
            ).fetchone()[0]
            assert held >= 1
    finally:
        conn.close()


def _frames(thread_id: str):
    async def _drain():
        return [f async for f in stream_turn(TEAM_A, A1, "status?", thread_id=thread_id)]
    return asyncio.run(_drain())


def test_a_turn_is_refused_while_its_thread_is_held(seeded):
    thread_id = _thread_id(TEAM_A)
    with thread_lock(thread_id) as held:
        assert held is True
        frames = _frames(thread_id)
    assert [f["type"] for f in frames] == ["busy"]
    assert "someone else" in frames[0]["detail"]


def test_a_private_turn_is_also_ordered(seeded):
    thread_id = _thread_id(TEAM_A, private=True)
    with thread_lock(thread_id) as held:
        assert held is True
        frames = _frames(thread_id)
    assert [f["type"] for f in frames] == ["busy"]
