"""What a watching browser and a starting worker cost the database.

🔴 THE DEFECTS.

`_run_frames` re-read EVERY step of a run every 200ms. A turn that had made
four hundred tool calls sent four hundred rows across the wire five times a
second, per viewer, to discover the one row that was new — and the cursor the
browser already sends was used to filter what it was TOLD, not what was
fetched.

The poll interval was a flat 200ms with no heartbeat, so a run parked between
steps cost the same as one producing them, and a connection that died silently
looked exactly like one where nothing was happening.

And `_pool()` was lazy, unsynchronised `if pool is None: create`. Two threads
arriving together both built a ConnectionPool; one won the dictionary and the
other's pool was orphaned — open, holding its minimum connections, absent from
`_pools`, and therefore missed by `close_pools()`. Harmless while the workers
were serial. T12 made them concurrent.
"""
import asyncio
import json
import threading

import psycopg

from agent.run_queue import enqueue_turn
from shared.config import settings
from tests._seed import A1, TEAM_A


def _thread_id() -> str:
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        row = conn.execute(
            "select id from public.threads where team_id=%s and title='General'",
            (TEAM_A,),
        ).fetchone()
    assert row is not None
    return str(row[0])


# ---------------------------------------------------------------------------
# Pools
# ---------------------------------------------------------------------------

def test_racing_threads_build_exactly_one_pool():
    """🔴 Two threads both saw None and both built one. The loser's pool was
    orphaned: open, holding connections, and invisible to close_pools()."""
    from shared import db

    url = settings.comrade_db_url_admin
    built: list = []
    real = db.ConnectionPool

    class _Counting(real):  # type: ignore[misc,valid-type]
        def __init__(self, *args, **kwargs):
            built.append(1)
            super().__init__(*args, **kwargs)

    db.ConnectionPool = _Counting  # type: ignore[assignment]
    existing = db._pools.pop(url, None)
    try:
        start = threading.Barrier(8)

        def _race():
            start.wait()
            db._pool(url)

        threads = [threading.Thread(target=_race) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(built) == 1, f"{len(built)} pools built for one url"
    finally:
        db.ConnectionPool = real  # type: ignore[assignment]
        made = db._pools.pop(url, None)
        if made is not None:
            made.close()
        if existing is not None:
            db._pools[url] = existing


def test_racing_threads_share_one_advisory_lock_pool():
    """Same shape, and this one holds up to 32 connections when it duplicates."""
    from shared import db

    built: list = []
    real = db.ConnectionPool

    class _Counting(real):  # type: ignore[misc,valid-type]
        def __init__(self, *args, **kwargs):
            built.append(1)
            super().__init__(*args, **kwargs)

    db.ConnectionPool = _Counting  # type: ignore[assignment]
    existing, db._lock_pool = db._lock_pool, None
    try:
        start = threading.Barrier(8)

        def _race():
            start.wait()
            db._advisory_lock_pool()

        threads = [threading.Thread(target=_race) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(built) == 1, f"{len(built)} lock pools built"
    finally:
        db.ConnectionPool = real  # type: ignore[assignment]
        if db._lock_pool is not None:
            db._lock_pool.close()
        db._lock_pool = existing


def test_the_process_says_how_many_connections_it_may_hold():
    """An operator sizing Postgres needs a number, not an archaeology exercise
    across five role pools and a lock pool."""
    from shared.db import max_connections

    assert max_connections() > 0


# ---------------------------------------------------------------------------
# Cursored replay
# ---------------------------------------------------------------------------

def _seed_steps(run_id: str, count: int) -> None:
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        for seq in range(count):
            conn.execute(
                "insert into public.agent_steps (run_id, team_id, seq, type, text)"
                " values (%s,%s,%s,'text',%s)",
                (run_id, TEAM_A, seq, f"step {seq}"),
            )
        conn.commit()


def test_a_run_read_can_start_after_a_cursor(seeded):
    """🔴 It could not, so every 200ms poll re-read the whole run."""
    from agent.run_queue import get_run

    run_id = str(enqueue_turn(TEAM_A, A1, _thread_id(), "long turn"))
    _seed_steps(run_id, 12)

    run = get_run(TEAM_A, run_id, after_seq=9)

    assert [s["seq"] for s in run["steps"]] == [10, 11]


def test_reading_without_a_cursor_still_returns_the_whole_run(seeded):
    from agent.run_queue import get_run

    run_id = str(enqueue_turn(TEAM_A, A1, _thread_id(), "long turn"))
    _seed_steps(run_id, 5)

    assert len(get_run(TEAM_A, run_id)["steps"]) == 5


def test_the_stream_asks_only_for_what_it_has_not_sent(seeded, monkeypatch):
    """The whole point: the cursor filtered what the browser was TOLD, while
    the database was still handing over every row each time."""
    import server.app as app_module

    asked: list[int] = []
    steps = [{"seq": i, "type": "text", "text": str(i)} for i in range(3)]

    def _fake_get_run(team_id, run_id, after_seq=-1):
        asked.append(after_seq)
        return {
            "id": run_id, "thread_id": "t", "requester_id": A1,
            "status": "done" if len(asked) > 1 else "running",
            "attempts": 1, "worker_id": "w", "lease_expires_at": None,
            "finished_at": None, "last_error": None,
            "steps": [s for s in steps if s["seq"] > after_seq],
        }

    monkeypatch.setattr(app_module, "get_run", _fake_get_run)
    # See fix.md F04: the stream revalidates before it emits.
    # These tests use a fabricated run id and are about polling
    # cost, so the check is stubbed; tests/test_stream_revocation.py
    # covers the check itself.
    monkeypatch.setattr(app_module, "_may_watch", lambda *_a, **_k: True)
    monkeypatch.setattr(app_module, "POLL_START_SECONDS", 0.0)
    monkeypatch.setattr(app_module, "POLL_MAX_SECONDS", 0.0)

    async def _go():
        return [line async for line in app_module._run_frames(TEAM_A, "run-1", viewer_id=A1)]

    asyncio.run(_go())

    assert asked[0] == -1
    assert asked[1] == 2, "the second read must start after what was sent"


# ---------------------------------------------------------------------------
# Polling and heartbeats
# ---------------------------------------------------------------------------

def test_a_quiet_run_is_polled_less_often_than_a_busy_one(monkeypatch):
    """🔴 A flat 200ms. A run waiting on a slow model call cost the database
    exactly as much as one producing a step every tick."""
    import server.app as app_module

    waits: list[float] = []

    async def _record(seconds):
        waits.append(seconds)
        if len(waits) > 6:
            raise RuntimeError("stop")

    monkeypatch.setattr(app_module.asyncio, "sleep", _record)
    monkeypatch.setattr(app_module, "get_run", lambda *_a, **_k: {
        "id": "run-1", "thread_id": "t", "requester_id": A1, "status": "running",
        "attempts": 1, "worker_id": "w", "lease_expires_at": None,
        "finished_at": None, "last_error": None, "steps": [],
    })
    # See fix.md F04: the stream revalidates access before it emits anything.
    # This run id and thread are fabricated, and these tests are about polling
    # cost; tests/test_stream_revocation.py covers the check itself.
    monkeypatch.setattr(app_module, "_may_watch", lambda *_a, **_k: True)

    async def _go():
        with_frames = []
        try:
            async for line in app_module._run_frames(TEAM_A, "run-1", viewer_id=A1):
                with_frames.append(line)
        except RuntimeError:
            pass
        return with_frames

    asyncio.run(_go())

    assert waits[0] < waits[-1], f"the wait never grew: {waits}"
    assert max(waits) <= app_module.POLL_MAX_SECONDS


def test_a_quiet_run_still_sends_a_heartbeat(monkeypatch):
    """Backing off must not make a dead connection indistinguishable from a
    slow one. Something has to go down the wire."""
    import server.app as app_module

    ticks = {"n": 0}

    async def _fast(_seconds):
        ticks["n"] += 1
        if ticks["n"] > 40:
            raise RuntimeError("stop")

    monkeypatch.setattr(app_module.asyncio, "sleep", _fast)
    monkeypatch.setattr(app_module, "get_run", lambda *_a, **_k: {
        "id": "run-1", "thread_id": "t", "requester_id": A1, "status": "running",
        "attempts": 1, "worker_id": "w", "lease_expires_at": None,
        "finished_at": None, "last_error": None, "steps": [],
    })
    # See fix.md F04: the stream revalidates access before it emits anything.
    # This run id and thread are fabricated, and these tests are about polling
    # cost; tests/test_stream_revocation.py covers the check itself.
    monkeypatch.setattr(app_module, "_may_watch", lambda *_a, **_k: True)

    async def _go():
        lines = []
        try:
            async for line in app_module._run_frames(TEAM_A, "run-1", viewer_id=A1):
                lines.append(line)
        except RuntimeError:
            pass
        return lines

    frames = [json.loads(line) for line in asyncio.run(_go()) if line.strip()]

    assert any(f["type"] == "heartbeat" for f in frames)


# ---------------------------------------------------------------------------
# Blocking work off the event loop
# ---------------------------------------------------------------------------

def test_no_async_endpoint_makes_a_blocking_database_call_directly():
    """🔴 The two STREAMING endpoints did — the ones a room holds open — so a
    database round trip ran on the event loop and every other request this
    worker was serving waited behind it.

    Static rather than behavioural on purpose: this is a rule about how the
    code is written, and a load test would only show it under a load nobody
    runs by accident."""
    import ast
    import pathlib

    #: Functions that open a database connection somewhere underneath.
    BLOCKING = {
        "get_run", "require_membership", "_resolve_thread", "get_thread_runs",
        "enqueue_turn", "cancel_run", "approve_consent", "reject_consent",
        "finalize_usage", "reserve_turn", "release_turn", "record_reservation",
        "_admit_turn", "_visible_run",
    }
    tree = ast.parse(pathlib.Path("server/app.py").read_text(encoding="utf-8"))

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id in BLOCKING):
                offenders.append(f"{node.name}:{inner.lineno} -> {inner.func.id}()")

    assert offenders == [], (
        "blocking database calls on the event loop: " + ", ".join(offenders)
        + " — wrap them in run_in_threadpool()"
    )
