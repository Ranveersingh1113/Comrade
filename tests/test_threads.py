import uuid

import psycopg
import pytest

from shared.config import settings
from tests._seed import A1, A2, B1, TEAM_A, TEAM_B, as_user


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def _thread(admin, team_id, *, visibility="team", title="Thread"):
    return admin.execute(
        "insert into public.threads (team_id, title, visibility, kind, created_by)"
        " values (%s,%s,%s,'discussion',%s) returning id",
        (team_id, title, visibility, A1 if team_id == TEAM_A else B1),
    ).fetchone()[0]


def test_legacy_messages_backfill_to_general_and_private_threads(seeded, admin):
    # Where a message LIVES is the whole claim now — thread_type is gone, so
    # the thread's own visibility and title are what say the backfill put each
    # message in the right place.
    rows = admin.execute(
        "select t.visibility, t.title from public.messages m"
        " join public.threads t on t.id = m.thread_id"
        " where m.team_id=%s order by t.title",
        (TEAM_A,),
    ).fetchall()
    assert rows == [("team", "General"), ("restricted", "Private")]


def test_restricted_thread_metadata_is_hidden_from_outsiders(seeded, admin):
    thread_id = _thread(admin, TEAM_A, visibility="restricted", title="A1 work")
    admin.execute(
        "insert into public.thread_participants (thread_id, team_id, user_id, added_by)"
        " values (%s,%s,%s,%s)", (thread_id, TEAM_A, A1, A1)
    )
    with as_user(A2) as conn:
        assert conn.execute("select id from public.threads where id=%s", (thread_id,)).fetchone() is None
    with as_user(A1) as conn:
        assert conn.execute("select id from public.threads where id=%s", (thread_id,)).fetchone() == (thread_id,)


def test_thread_child_cannot_cross_team(seeded, admin):
    thread_id = _thread(admin, TEAM_A)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        admin.execute(
            "insert into public.thread_participants (thread_id, team_id, user_id, added_by)"
            " values (%s,%s,%s,%s)", (thread_id, TEAM_B, B1, B1)
        )


def test_thread_participant_must_be_active_team_member(seeded, admin):
    thread_id = _thread(admin, TEAM_A, visibility="restricted")
    with pytest.raises(psycopg.errors.RaiseException, match="active team member"):
        admin.execute(
            "insert into public.thread_participants (thread_id, team_id, user_id, added_by)"
            " values (%s,%s,%s,%s)", (thread_id, TEAM_A, B1, A1)
        )


def test_member_can_create_restricted_thread_and_add_themselves(seeded):
    thread_id = str(uuid.uuid4())
    with as_user(A1, commit=True) as conn:
        conn.execute(
            "insert into public.threads (id, team_id, title, visibility, kind, created_by)"
            " values (%s,%s,'Private work','restricted','discussion',%s)",
            (thread_id, TEAM_A, A1),
        )
        conn.execute(
            "insert into public.thread_participants (thread_id, team_id, user_id, added_by)"
            " values (%s,%s,%s,%s)", (thread_id, TEAM_A, A1, A1)
        )
        assert conn.execute(
            "select id from public.threads where id=%s", (thread_id,)
        ).fetchone() == (uuid.UUID(thread_id),)


def test_first_agent_message_titles_a_new_public_thread(seeded, admin):
    thread_id = _thread(admin, TEAM_A, title="New thread")
    with as_user(A1, commit=True) as conn:
        conn.execute(
            "select * from public.enqueue_agent_turn(%s,%s,%s)",
            (TEAM_A, thread_id, "  Review   the release checklist.  "),
        ).fetchone()
    title = admin.execute(
        "select title from public.threads where id=%s", (thread_id,)
    ).fetchone()[0]
    assert title == "Review the release checklist."


def test_removing_participant_revokes_thread_message_access(seeded, admin):
    thread_id = _thread(admin, TEAM_A, visibility="restricted")
    for user_id in (A1, A2):
        admin.execute(
            "insert into public.thread_participants (thread_id, team_id, user_id, added_by)"
            " values (%s,%s,%s,%s)", (thread_id, TEAM_A, user_id, A1)
        )
    message_id = admin.execute(
        "insert into public.messages (team_id, thread_id,"
        " sender_kind, sender_id, body)"
        " values (%s,%s,'user',%s,'restricted') returning id",
        (TEAM_A, thread_id, A1),
    ).fetchone()[0]
    with as_user(A2) as conn:
        assert conn.execute("select id from public.messages where id=%s", (message_id,)).fetchone()
    with as_user(A1, commit=True) as conn:
        conn.execute(
            "delete from public.thread_participants where thread_id=%s and user_id=%s",
            (thread_id, A2),
        )
    with as_user(A2) as conn:
        assert conn.execute("select id from public.messages where id=%s", (message_id,)).fetchone() is None


def test_former_creator_cannot_manage_thread_participants(seeded, admin):
    thread_id = _thread(admin, TEAM_A, visibility="restricted")
    for user_id in (A1, A2):
        admin.execute(
            "insert into public.thread_participants (thread_id, team_id, user_id, added_by)"
            " values (%s,%s,%s,%s)", (thread_id, TEAM_A, user_id, A1)
        )
    admin.execute(
        "update public.memberships set status='left' where team_id=%s and user_id=%s",
        (TEAM_A, A1),
    )
    with as_user(A1) as conn:
        deleted = conn.execute(
            "delete from public.thread_participants where thread_id=%s and user_id=%s",
            (thread_id, A2),
        )
        assert deleted.rowcount == 0
    assert admin.execute(
        "select 1 from public.thread_participants where thread_id=%s and user_id=%s",
        (thread_id, A2),
    ).fetchone()
    admin.execute(
        "delete from public.thread_participants where thread_id=%s and user_id=%s",
        (thread_id, A2),
    )
    with as_user(A1) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "insert into public.thread_participants (thread_id, team_id, user_id, added_by)"
                " values (%s,%s,%s,%s)", (thread_id, TEAM_A, A2, A1)
            )
    assert admin.execute(
        "select 1 from public.thread_participants where thread_id=%s and user_id=%s",
        (thread_id, A2),
    ).fetchone() is None


def test_a_message_cannot_be_moved_to_another_thread_after_insert(seeded, admin):
    """The rule outlived its wording.

    This asserted that thread_type/thread_owner_id were immutable — a message
    could not be reclassified from group to private after the fact. Those
    columns went with the contract migration, but the rule did not:
    trg_thread_identity_guard now covers thread_id alongside team_id,
    sender_id and sender_kind. Moving a message between threads is the same
    act the old version forbade, so the test follows the rule rather than
    retiring with the column.

    (Its neighbour, test_legacy_insert_receives_canonical_thread_id, is gone.
    That one tested the temporary trigger mapping legacy inserts onto
    canonical threads — scaffolding Task 8 removed on purpose, so there is
    nothing left for it to protect.)
    """
    general = admin.execute(
        "select id from public.threads where team_id=%s and title='General'",
        (TEAM_A,),
    ).fetchone()[0]
    other = _thread(admin, TEAM_A, title="Release work")
    message_id = admin.execute(
        "insert into public.messages (team_id, thread_id, sender_kind, sender_id, body)"
        " values (%s,%s,'user',%s,'team message') returning id",
        (TEAM_A, general, A1),
    ).fetchone()[0]
    with as_user(A1) as conn, pytest.raises(psycopg.errors.RaiseException, match="identity"):
        conn.execute(
            "update public.messages set thread_id=%s where id=%s",
            (other, message_id),
        )
