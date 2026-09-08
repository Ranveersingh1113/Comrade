"""Access that ends while somebody is still watching.

🔴 THE DEFECT (fix.md F04). `agent_run_stream` checked membership and thread
visibility ONCE, when the connection opened. `_run_frames` then polled with
privileged, unattributed reads — it asked the database for the run by id and
streamed whatever came back — for as long as the stream stayed open, which on a
long turn is the whole turn.

So removing somebody from a restricted thread, or from the team, did not stop
new private steps reaching the browser they already had open. Revocation
applied to the next connection and not to the one in flight, which is the one
that matters: a member being removed mid-turn is exactly when the next tool
call is worth reading.
"""
import asyncio
import json

import psycopg
import pytest

from server.app import _run_frames
from shared.agent_runs import append_step, start_run
from shared.config import settings
from tests._seed import A1, A2, TEAM_A


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


@pytest.fixture
def restricted_run(seeded):
    """A run inside a thread only A1 may open."""
    conn = _admin()
    try:
        thread_id = conn.execute(
            "insert into public.threads (team_id, title, visibility, kind,"
            " created_by) values (%s,'Security review','restricted',"
            "'discussion',%s) returning id", (TEAM_A, A1),
        ).fetchone()[0]
        for member in (A1, A2):
            conn.execute(
                "insert into public.thread_participants (thread_id, team_id,"
                " user_id, added_by) values (%s,%s,%s,%s)",
                (thread_id, TEAM_A, member, A1),
            )
    finally:
        conn.close()
    run_id = start_run(TEAM_A, A1, str(thread_id), None, "user", "look at this")
    return str(thread_id), str(run_id)


def _drop_participant(thread_id: str, user_id: str) -> None:
    conn = _admin()
    try:
        conn.execute(
            "delete from public.thread_participants where thread_id=%s"
            " and user_id=%s", (thread_id, user_id),
        )
    finally:
        conn.close()


def _remove_from_team(user_id: str) -> None:
    conn = _admin()
    try:
        conn.execute(
            "delete from public.memberships where team_id=%s and user_id=%s",
            (TEAM_A, user_id),
        )
    finally:
        conn.close()


def _watch(run_id: str, viewer: str, revoke) -> list[dict]:
    """Open a stream, revoke access, add a private step, collect the frames."""
    async def _go():
        frames: list[dict] = []
        agen = _run_frames(TEAM_A, run_id, viewer_id=viewer)
        # The first frame proves the stream opened for this viewer.
        frames.append(json.loads(await anext(agen)))

        revoke()
        await asyncio.to_thread(
            append_step, TEAM_A, run_id,
            {"seq": 99, "type": "text", "text": "the private next step"},
        )

        try:
            async with asyncio.timeout(20):
                async for line in agen:
                    frames.append(json.loads(line))
                    if len(frames) > 12:
                        break
        except TimeoutError:
            pass
        finally:
            await agen.aclose()
        return frames

    return asyncio.run(_go())


# ---------------------------------------------------------------------------

def test_a_removed_participant_stops_receiving_private_steps(restricted_run):
    """🔴 They did not. The check ran at connect and never again."""
    thread_id, run_id = restricted_run

    frames = _watch(run_id, A2, lambda: _drop_participant(thread_id, A2))

    leaked = [f for f in frames if f.get("text") == "the private next step"]
    assert not leaked, "a removed participant received the next private step"


def test_the_stream_says_why_it_ended(restricted_run):
    """A stream that simply stops is indistinguishable from a hang, which is
    the failure this codebase keeps having to fix."""
    thread_id, run_id = restricted_run

    frames = _watch(run_id, A2, lambda: _drop_participant(thread_id, A2))

    assert frames[-1]["type"] == "error", frames[-1]
    assert "access" in frames[-1]["detail"]


def test_removal_from_the_team_also_closes_it(restricted_run):
    """Thread visibility is one boundary; membership is the one underneath."""
    thread_id, run_id = restricted_run

    frames = _watch(run_id, A2, lambda: _remove_from_team(A2))

    assert not [f for f in frames if f.get("text") == "the private next step"]


def test_a_participant_who_still_belongs_keeps_receiving(restricted_run):
    """The other half. A revalidation that closed everybody's stream would
    pass every test above and break the product."""
    thread_id, run_id = restricted_run

    frames = _watch(run_id, A1, lambda: _drop_participant(thread_id, A2))

    assert [f for f in frames if f.get("text") == "the private next step"], (
        "the remaining authorized viewer stopped receiving output"
    )
