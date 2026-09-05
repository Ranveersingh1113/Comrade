"""Every write the product performs, exercised the way the product performs it.

The audit that produced this file, 2026-08-31.

Two RLS holes shipped and were found by accident, and both shared one root:
**the tests exercised the DATABASE, not the PRODUCT.**

  * `INSERT` and `INSERT ... RETURNING` behave differently under RLS —
    RETURNING makes the new row pass the SELECT policy as well as the INSERT
    one. Every fixture used the bare form; the client uses the other. Team
    creation was therefore broken in the app while green in the suite.
  * The e2e fixtures seed teams with admin SQL, so no test had ever created a
    team the way a person does.

That is three RETURNING-under-RLS surprises in one week (`_persist_ai_reply`
and `propose_action` were the other two), which makes it a blind spot rather
than a coincidence.

So this file enumerates every RLS-gated write in `frontend/src` and performs it
as a real member, in the same shape the client issues it — `.select()` after
`.insert()` means RETURNING, and that is what is written here. It is a
boundary test, not a feature test: it asserts the door opens for the people it
should and stays shut for everyone else.

Keep it in step with the client. If a screen gains a write, it gains a case.
"""
import psycopg
import pytest

from shared.config import settings
from tests._seed import A1, A2, B1, TEAM_A, TEAM_B, as_user, general_thread


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# state/AuthContext.tsx — first sign-in materialises a profile
# ---------------------------------------------------------------------------

def test_a_member_can_upsert_their_own_profile_with_returning(seeded):
    """`.insert(...).select()` — RETURNING, so au_profiles_select must pass."""
    with as_user(A1) as conn:
        row = conn.execute(
            "insert into public.profiles (id, display_name, email)"
            " values (%s,'A One','a1@test.dev')"
            " on conflict (id) do update set display_name = excluded.display_name"
            " returning id",
            (A1,),
        ).fetchone()
    assert row is not None


def test_a_member_cannot_write_someone_elses_profile(seeded):
    with pytest.raises(psycopg.Error):
        with as_user(A1) as conn:
            conn.execute(
                "insert into public.profiles (id, display_name) values (%s,'hijack')"
                " on conflict (id) do update set display_name='hijack'",
                (A2,),
            )


# ---------------------------------------------------------------------------
# screens/TeamGate.tsx — found a team, then accept an invite
# ---------------------------------------------------------------------------

def test_founding_a_team_the_way_the_client_does(seeded, admin):
    """Both statements, both with RETURNING, in the client's order."""
    with as_user(B1, commit=True) as conn:
        team_id = conn.execute(
            "insert into public.teams (name, created_by) values ('audit team',%s)"
            " returning id",
            (B1,),
        ).fetchone()[0]
        conn.execute(
            "insert into public.memberships (team_id, user_id, role, status,"
            " joined_at) values (%s,%s,'leader','active',now())",
            (team_id, B1),
        )
    try:
        assert admin.execute(
            "select count(*) from public.memberships where team_id=%s", (team_id,)
        ).fetchone()[0] == 1
    finally:
        admin.execute("delete from public.teams where id=%s", (team_id,))


def test_accepting_an_invite(seeded, admin):
    """TeamGate: update your own membership to active."""
    admin.execute(
        "insert into public.memberships (team_id, user_id, role, status)"
        " values (%s,%s,'member','invited')",
        (TEAM_A, B1),
    )
    with as_user(B1, commit=True) as conn:
        conn.execute(
            "update public.memberships set status='active', joined_at=now()"
            " where team_id=%s and user_id=%s",
            (TEAM_A, B1),
        )
    assert admin.execute(
        "select status from public.memberships where team_id=%s and user_id=%s",
        (TEAM_A, B1),
    ).fetchone()[0] == "active"


# ---------------------------------------------------------------------------
# screens/Setup.tsx and screens/Documents.tsx — upload a document
# ---------------------------------------------------------------------------

def test_a_member_can_upload_a_document_with_returning(seeded):
    """Both upload screens do `.insert(...).select().single()`."""
    with as_user(A1) as conn:
        row = conn.execute(
            "insert into public.documents (team_id, uploader_id, kind, filename,"
            " storage_path, status) values (%s,%s,'text','brief.txt','p/brief.txt',"
            " 'uploaded') returning id",
            (TEAM_A, A1),
        ).fetchone()
    assert row is not None


def test_a_member_cannot_upload_into_another_team(seeded):
    with pytest.raises(psycopg.Error):
        with as_user(B1) as conn:
            conn.execute(
                "insert into public.documents (team_id, uploader_id, kind, filename,"
                " status) values (%s,%s,'text','sneak.txt','uploaded')",
                (TEAM_A, B1),
            )


# ---------------------------------------------------------------------------
# screens/GroupRoom.tsx — post, and tombstone your own message
# ---------------------------------------------------------------------------

def test_a_member_can_post_to_their_own_team(seeded):
    with as_user(A1) as conn:
        conn.execute(
            "insert into public.messages (team_id, thread_id, sender_kind,"
            " sender_id, body) values (%s,%s,'user',%s,'hello')",
            (TEAM_A, general_thread(conn, TEAM_A), A1),
        )


def test_a_member_cannot_post_as_someone_else(seeded):
    with pytest.raises(psycopg.Error):
        with as_user(A1) as conn:
            conn.execute(
                "insert into public.messages (team_id, thread_id, sender_kind,"
                " sender_id, body) values (%s,%s,'user',%s,'not me')",
                (TEAM_A, general_thread(conn, TEAM_A), A2),
            )


def test_a_member_can_tombstone_their_own_message(seeded, admin):
    mid = admin.execute(
        "insert into public.messages (team_id, thread_id, sender_kind, sender_id,"
        " body) values (%s,%s,'user',%s,'mine') returning id",
        (TEAM_A, general_thread(admin, TEAM_A), A1),
    ).fetchone()[0]
    with as_user(A1, commit=True) as conn:
        conn.execute(
            "update public.messages set deleted_scope='everyone', deleted_by=%s,"
            " deleted_at=now() where id=%s",
            (A1, mid),
        )
    assert admin.execute(
        "select deleted_scope from public.messages where id=%s", (mid,)
    ).fetchone()[0] == "everyone"


# ---------------------------------------------------------------------------
# hooks/useTasks.ts — create and advance a task
# ---------------------------------------------------------------------------

def test_a_member_can_create_a_task(seeded):
    with as_user(A1) as conn:
        conn.execute(
            "insert into public.tasks (team_id, assignee_id, title, created_by_kind,"
            " created_by_id) values (%s,%s,'write the brief','user',%s)",
            (TEAM_A, A2, A1),
        )


def test_only_the_assignee_confirms_their_task(seeded, admin):
    """The product's core task invariant, enforced by trg_tasks_confirm_guard."""
    tid = admin.execute(
        "insert into public.tasks (team_id, assignee_id, title, created_by_kind,"
        " created_by_id) values (%s,%s,'ship it','user',%s) returning id",
        (TEAM_A, A2, A1),
    ).fetchone()[0]
    with pytest.raises(psycopg.Error):
        with as_user(A1) as conn:  # A1 is not the assignee
            conn.execute(
                "update public.tasks set status='confirmed', confirmed_at=now()"
                " where id=%s",
                (tid,),
            )
    with as_user(A2, commit=True) as conn:  # A2 is
        conn.execute(
            "update public.tasks set status='confirmed', confirmed_at=now()"
            " where id=%s",
            (tid,),
        )
    assert admin.execute(
        "select status from public.tasks where id=%s", (tid,)
    ).fetchone()[0] == "confirmed"


# ---------------------------------------------------------------------------
# screens/Wiki.tsx and components/MemoryDiffCard.tsx — queue a revert
# ---------------------------------------------------------------------------

def test_a_member_can_queue_a_revert(seeded):
    """Members write HERE and never to memory_* itself — the sole-writer rule."""
    from tests._seed import ENTRY_A, VER_A

    with as_user(A1) as conn:
        conn.execute(
            "insert into public.memory_reverts (entry_id, team_id, member_id,"
            " reverted_version_id) values (%s,%s,%s,%s)",
            (ENTRY_A, TEAM_A, A1, VER_A),
        )


def test_a_member_still_cannot_write_memory_directly(seeded):
    """If this ever passes, the compiler has stopped being the sole writer."""
    with pytest.raises(psycopg.Error):
        with as_user(A1) as conn:
            conn.execute(
                "insert into public.memory_entries (team_id) values (%s)", (TEAM_A,)
            )


# ---------------------------------------------------------------------------
# The boundary itself
# ---------------------------------------------------------------------------

def test_a_member_cannot_move_their_membership_to_another_team(seeded, admin):
    """Measured, and recorded as measured.

    au_memberships_update is `(user_id = auth.uid()) OR is_team_leader(team_id)`
    on both USING and CHECK, which by inspection ought to permit this: the new
    row still has user_id = auth.uid(). Postgres rejects it anyway, and
    consistently — a same-value write to team_id succeeds while a cross-team
    one does not.

    I could not account for the difference from the policy text alone, so this
    pins the BEHAVIOUR rather than an explanation. If a future change makes it
    pass, that is a cross-tenant teleport and this test is the alarm, whatever
    the mechanism turns out to have been.
    """
    with pytest.raises(psycopg.Error):
        with as_user(B1) as conn:
            conn.execute(
                "update public.memberships set team_id=%s where user_id=%s"
                " and team_id=%s",
                (TEAM_A, B1, TEAM_B),
            )
