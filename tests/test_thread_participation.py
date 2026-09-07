"""Joining and leaving a thread, and what each leaves behind.

🔴 THE DEFECT. `thread_participants` records `added_by` and `joined_at`, so an
addition is audited. A REMOVAL deletes the row and takes the only evidence with
it — and removal is the consequential half: it revokes a person's access to the
thread's entire history, its runs, its approvals and its previews. Nothing
recorded who did that, or when.
"""
import psycopg
import pytest

from shared.config import settings
from tests._seed import A1, A2, B1, TEAM_A, as_user


def _restricted_thread(owner: str, title: str = "Design review") -> str:
    """A restricted thread created by `owner`, with `owner` participating."""
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        row = conn.execute(
            "insert into public.threads (team_id, title, visibility, kind, created_by)"
            " values (%s,%s,'restricted','discussion',%s) returning id",
            (TEAM_A, title, owner),
        ).fetchone()
        thread_id = str(row[0])
        conn.execute(
            "insert into public.thread_participants (thread_id, team_id, user_id, added_by)"
            " values (%s,%s,%s,%s)",
            (thread_id, TEAM_A, owner, owner),
        )
        conn.commit()
    return thread_id


def _events(thread_id: str) -> list[tuple]:
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        return conn.execute(
            "select action, user_id::text, actor_id::text"
            " from public.thread_participant_events"
            " where thread_id=%s order by at, action",
            (thread_id,),
        ).fetchall()


def test_adding_someone_is_recorded_with_who_did_it(seeded):
    thread_id = _restricted_thread(A1)

    with as_user(A1, commit=True) as conn:
        conn.execute(
            "insert into public.thread_participants (thread_id, team_id, user_id, added_by)"
            " values (%s,%s,%s,%s)",
            (thread_id, TEAM_A, A2, A1),
        )

    added = [e for e in _events(thread_id) if e[1] == A2]
    assert added == [("added", A2, A1)]


def test_removing_someone_is_recorded_too(seeded):
    """🔴 It was not. The row was deleted and nothing said it had existed."""
    thread_id = _restricted_thread(A1)
    with as_user(A1, commit=True) as conn:
        conn.execute(
            "insert into public.thread_participants (thread_id, team_id, user_id, added_by)"
            " values (%s,%s,%s,%s)",
            (thread_id, TEAM_A, A2, A1),
        )
    with as_user(A1, commit=True) as conn:
        conn.execute(
            "delete from public.thread_participants where thread_id=%s and user_id=%s",
            (thread_id, A2),
        )

    actions = [e[0] for e in _events(thread_id) if e[1] == A2]
    assert actions == ["added", "removed"]
    removal = [e for e in _events(thread_id) if e[1] == A2 and e[0] == "removed"][0]
    assert removal[2] == A1, "the record must say who did it"


def test_removal_revokes_the_thread_immediately(seeded):
    """The audit is the new part; this is the guarantee it is auditing."""
    thread_id = _restricted_thread(A1)
    with as_user(A1, commit=True) as conn:
        conn.execute(
            "insert into public.thread_participants (thread_id, team_id, user_id, added_by)"
            " values (%s,%s,%s,%s)",
            (thread_id, TEAM_A, A2, A1),
        )
    with as_user(A2) as conn:
        assert conn.execute(
            "select 1 from public.threads where id=%s", (thread_id,)
        ).fetchone() is not None

    with as_user(A1, commit=True) as conn:
        conn.execute(
            "delete from public.thread_participants where thread_id=%s and user_id=%s",
            (thread_id, A2),
        )

    with as_user(A2) as conn:
        assert conn.execute(
            "select 1 from public.threads where id=%s", (thread_id,)
        ).fetchone() is None


def test_someone_removed_cannot_read_the_record_of_their_removal(seeded):
    """It follows the thread's own rule rather than being an exception to it."""
    thread_id = _restricted_thread(A1)
    with as_user(A1, commit=True) as conn:
        conn.execute(
            "insert into public.thread_participants (thread_id, team_id, user_id, added_by)"
            " values (%s,%s,%s,%s)",
            (thread_id, TEAM_A, A2, A1),
        )
    with as_user(A1, commit=True) as conn:
        conn.execute(
            "delete from public.thread_participants where thread_id=%s and user_id=%s",
            (thread_id, A2),
        )

    with as_user(A2) as conn:
        rows = conn.execute(
            "select 1 from public.thread_participant_events where thread_id=%s",
            (thread_id,),
        ).fetchall()
    assert rows == []


def test_a_participant_can_read_the_roster_history(seeded):
    thread_id = _restricted_thread(A1)
    with as_user(A1, commit=True) as conn:
        conn.execute(
            "insert into public.thread_participants (thread_id, team_id, user_id, added_by)"
            " values (%s,%s,%s,%s)",
            (thread_id, TEAM_A, A2, A1),
        )

    with as_user(A2) as conn:
        rows = conn.execute(
            "select action from public.thread_participant_events where thread_id=%s",
            (thread_id,),
        ).fetchall()
    assert len(rows) >= 1


def test_another_team_never_sees_any_of_it(seeded):
    thread_id = _restricted_thread(A1)

    with as_user(B1) as conn:
        rows = conn.execute(
            "select 1 from public.thread_participant_events where thread_id=%s",
            (thread_id,),
        ).fetchall()
    assert rows == []


def test_the_record_outlives_the_thread(seeded):
    """A cascading thread delete must not erase the record of every removal
    that led up to it — the same reason sandbox_cleanup has no foreign key."""
    thread_id = _restricted_thread(A1)
    with as_user(A1, commit=True) as conn:
        conn.execute(
            "insert into public.thread_participants (thread_id, team_id, user_id, added_by)"
            " values (%s,%s,%s,%s)",
            (thread_id, TEAM_A, A2, A1),
        )
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        conn.execute("delete from public.threads where id=%s", (thread_id,))
        conn.commit()

    assert _events(thread_id) != []


@pytest.mark.parametrize("table", ["threads", "thread_participants"])
def test_thread_changes_are_published_for_realtime(seeded, table):
    """🔴 Neither was published, so a teammate creating a thread, renaming one,
    or adding somebody to one was invisible until a reload."""
    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        row = conn.execute(
            "select 1 from pg_publication_tables where pubname='supabase_realtime'"
            " and schemaname='public' and tablename=%s",
            (table,),
        ).fetchone()
    assert row is not None
