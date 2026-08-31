"""A team is a thing you can leave, and nobody can push you out.

D4. Before this, `au_memberships_delete` was `is_team_leader(team_id)` and
`memberships.status` was `('invited','active')` — so there was no way for a
member to leave a team, and the only exit was the leader deleting your row.
Both halves are wrong for this product:

  * **Nobody could leave.** A member who joined was in permanently. That is the
    plain defect, and the one a real person hits first.
  * **The leader could remove you.** §23.1 is explicit — *"nobody approves
    another member's actions, nobody configures permissions."* An admin power
    to eject a peer is the exact shape this product exists not to have.

So leaving is self-service and unilateral, and being removed is not something
that happens TO you: another member can only ASK, and your own key resolves it
(tests/test_departure_request.py).

`status='left'` rather than a DELETE, for the codebase's own reason —
deletion leaves a trace. A departed member's messages and tasks keep an author
the roster can still name (GroupRoom.tsx already renders 'Former member' when
a profile is missing), and the row is the record that they were here.
"""
import psycopg
import pytest

from shared.config import settings
from shared.db import Role, team_session
from tests._seed import A1, A2, B1, TEAM_A, TEAM_B, as_user


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def _leave(uid, team_id):
    with as_user(uid, commit=True) as conn:
        conn.execute(
            "update public.memberships set status='left', left_at=now()"
            " where team_id=%s and user_id=%s",
            (team_id, uid),
        )


# ---------------------------------------------------------------------------
# Leaving
# ---------------------------------------------------------------------------

def test_a_member_can_leave(seeded, admin):
    _leave(A2, TEAM_A)
    assert admin.execute(
        "select status from public.memberships where team_id=%s and user_id=%s",
        (TEAM_A, A2),
    ).fetchone()[0] == "left"


def test_leaving_closes_the_door_behind_you(seeded):
    """The whole point: after leaving, the team's contents are gone."""
    with as_user(A2) as conn:
        assert conn.execute(
            "select count(*) from public.messages where team_id=%s", (TEAM_A,)
        ).fetchone()[0] > 0, "precondition: A2 can read the room while a member"

    _leave(A2, TEAM_A)

    with as_user(A2) as conn:
        assert conn.execute(
            "select count(*) from public.messages where team_id=%s", (TEAM_A,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "select count(*) from public.tasks where team_id=%s", (TEAM_A,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "select count(*) from public.memory_versions where team_id=%s", (TEAM_A,)
        ).fetchone()[0] == 0


def test_a_departed_member_no_longer_sees_the_team_at_all(seeded):
    """Not even its name.

    au_teams_select lets you see a team that has INVITED you, through
    has_membership — which by design ignores status so an invitee can read the
    name of the team inviting them. 'left' must not ride in on that: the team
    would keep appearing in the picker forever with no way to open it.
    """
    _leave(A2, TEAM_A)
    with as_user(A2) as conn:
        names = {r[0] for r in conn.execute("select name from public.teams").fetchall()}
    assert "Team A" not in names


def test_leaving_touches_nobody_else(seeded):
    _leave(A2, TEAM_A)
    with as_user(A1) as conn:
        assert conn.execute(
            "select count(*) from public.messages where team_id=%s", (TEAM_A,)
        ).fetchone()[0] > 0
        assert conn.execute(
            "select count(*) from public.memberships"
            " where team_id=%s and status='active'",
            (TEAM_A,),
        ).fetchone()[0] == 1


def test_the_departed_row_survives_as_the_trace(seeded):
    """A2's authorship stays resolvable; only their access is gone."""
    _leave(A2, TEAM_A)
    with as_user(A1) as conn:
        row = conn.execute(
            "select status, left_at from public.memberships"
            " where team_id=%s and user_id=%s",
            (TEAM_A, A2),
        ).fetchone()
    assert row is not None and row[0] == "left" and row[1] is not None


# ---------------------------------------------------------------------------
# The hole that adding 'left' would otherwise open
# ---------------------------------------------------------------------------

def test_a_member_who_left_cannot_walk_back_in(seeded):
    """au_memberships_update lets you write your OWN row. Without a transition
    guard that is self-service re-entry: set status back to 'active' and every
    policy in the schema believes you again."""
    _leave(A2, TEAM_A)
    with pytest.raises(psycopg.Error):
        with as_user(A2) as conn:
            conn.execute(
                "update public.memberships set status='active'"
                " where team_id=%s and user_id=%s",
                (TEAM_A, A2),
            )


def test_a_member_who_left_cannot_re_found_the_team(seeded):
    """The other way back in — through the founding branch.

    is_unfounded_team is `you created it AND it has no active members`. Once
    the creator leaves an empty team both halves are true again, so the
    founding INSERT would re-admit them — to a team now full of other people's
    history. The unique constraint blocks the INSERT and the transition guard
    blocks the UPDATE; this asserts BOTH doors, because either one alone
    leaves the other open.
    """
    _leave(A2, TEAM_A)
    _leave(A1, TEAM_A)  # A1 created Team A

    with as_user(A1) as conn:
        assert conn.execute(
            "select public.is_unfounded_team(%s)", (TEAM_A,)
        ).fetchone()[0] is True, "precondition: the founding branch is open again"

    with pytest.raises(psycopg.Error):
        with as_user(A1) as conn:
            conn.execute(
                "insert into public.memberships (team_id, user_id, role, status)"
                " values (%s,%s,'leader','active')",
                (TEAM_A, A1),
            )
    with pytest.raises(psycopg.Error):
        with as_user(A1) as conn:
            conn.execute(
                "update public.memberships set status='active'"
                " where team_id=%s and user_id=%s",
                (TEAM_A, A1),
            )


# ---------------------------------------------------------------------------
# Nobody can push you out
# ---------------------------------------------------------------------------

def test_the_leader_cannot_make_you_leave(seeded):
    """§23.1. A1 is Team A's leader and that must buy exactly nothing here."""
    with pytest.raises(psycopg.Error):
        with as_user(A1) as conn:
            conn.execute(
                "update public.memberships set status='left', left_at=now()"
                " where team_id=%s and user_id=%s",
                (TEAM_A, A2),
            )


def test_the_leader_cannot_delete_an_active_member(seeded):
    """The old exit: au_memberships_delete was leader-only and unrestricted.

    A silent zero-row DELETE is the pass here, not an error — a row the USING
    clause rejects simply is not there to delete. So this asserts the row
    survives rather than that the statement raised.
    """
    with as_user(A1, commit=True) as conn:
        conn.execute(
            "delete from public.memberships where team_id=%s and user_id=%s",
            (TEAM_A, A2),
        )
    with as_user(A1) as conn:
        assert conn.execute(
            "select count(*) from public.memberships where team_id=%s and user_id=%s",
            (TEAM_A, A2),
        ).fetchone()[0] == 1, "an active member was deleted by the leader"


def test_a_stranger_cannot_make_you_leave(seeded):
    with as_user(B1, commit=True) as conn:
        conn.execute(
            "update public.memberships set status='left'"
            " where team_id=%s and user_id=%s",
            (TEAM_A, A2),
        )
    with as_user(A1) as conn:
        assert conn.execute(
            "select status from public.memberships where team_id=%s and user_id=%s",
            (TEAM_A, A2),
        ).fetchone()[0] == "active"


# ---------------------------------------------------------------------------
# Invitations: withdrawing and declining are not removals
# ---------------------------------------------------------------------------

def test_a_leader_can_withdraw_an_invitation(seeded, admin):
    """Deleting an invitation is not ejecting a member — nobody has joined."""
    admin.execute(
        "insert into public.memberships (team_id, user_id, role, status)"
        " values (%s,%s,'member','invited')",
        (TEAM_A, B1),
    )
    with as_user(A1, commit=True) as conn:
        conn.execute(
            "delete from public.memberships where team_id=%s and user_id=%s",
            (TEAM_A, B1),
        )
    assert admin.execute(
        "select count(*) from public.memberships where team_id=%s and user_id=%s",
        (TEAM_A, B1),
    ).fetchone()[0] == 0


def test_an_invitee_can_decline(seeded, admin):
    admin.execute(
        "insert into public.memberships (team_id, user_id, role, status)"
        " values (%s,%s,'member','invited')",
        (TEAM_A, B1),
    )
    with as_user(B1, commit=True) as conn:
        conn.execute(
            "delete from public.memberships where team_id=%s and user_id=%s",
            (TEAM_A, B1),
        )
    assert admin.execute(
        "select count(*) from public.memberships where team_id=%s and user_id=%s",
        (TEAM_A, B1),
    ).fetchone()[0] == 0


def test_a_departed_member_can_be_invited_back(seeded):
    """Leaving is not a ban. The leader re-invites; the row goes left→invited.

    This is the one transition on someone else's row a leader may make, and it
    is why server/invites.py had to stop saying `on conflict do nothing` —
    otherwise a departed member is reported 'already_member' forever and can
    never come back.
    """
    _leave(A2, TEAM_A)
    with as_user(A1, commit=True) as conn:
        conn.execute(
            "update public.memberships set status='invited', left_at=null"
            " where team_id=%s and user_id=%s",
            (TEAM_A, A2),
        )
    with as_user(A2, commit=True) as conn:
        conn.execute(
            "update public.memberships set status='active', joined_at=now()"
            " where team_id=%s and user_id=%s",
            (TEAM_A, A2),
        )
    with as_user(A2) as conn:
        assert conn.execute(
            "select count(*) from public.messages where team_id=%s", (TEAM_A,)
        ).fetchone()[0] > 0


# ---------------------------------------------------------------------------
# Deleting a team: the last member out turns off the lights
# ---------------------------------------------------------------------------

def test_the_team_is_archived_when_the_last_member_leaves(seeded, admin):
    """There is no delete-team button, by design.

    A team with no active members is already invisible to everyone —
    is_team_member is false for all of them — so a delete would be a second
    mechanism for a state the schema reaches on its own, and an admin power
    besides. What was missing is the TRACE: archived_at says the team ended
    and when, rather than leaving a row that merely looks abandoned. §23.3
    keeps the history either way.
    """
    assert admin.execute(
        "select archived_at from public.teams where id=%s", (TEAM_A,)
    ).fetchone()[0] is None

    _leave(A2, TEAM_A)
    assert admin.execute(
        "select archived_at from public.teams where id=%s", (TEAM_A,)
    ).fetchone()[0] is None, "archived while a member was still in it"

    _leave(A1, TEAM_A)
    assert admin.execute(
        "select archived_at from public.teams where id=%s", (TEAM_A,)
    ).fetchone()[0] is not None


def test_archiving_a_team_leaves_its_neighbours_alone(seeded, admin):
    _leave(A1, TEAM_A)
    _leave(A2, TEAM_A)
    assert admin.execute(
        "select archived_at from public.teams where id=%s", (TEAM_B,)
    ).fetchone()[0] is None


def test_re_joining_un_archives_the_team(seeded, admin):
    """archived_at is a state, not a gravestone: it must clear if the team
    comes back, or a re-invited member walks into a team the schema still
    describes as ended."""
    _leave(A2, TEAM_A)
    _leave(A1, TEAM_A)
    admin.execute(
        "update public.memberships set status='invited'"
        " where team_id=%s and user_id=%s",
        (TEAM_A, A1),
    )
    with as_user(A1, commit=True) as conn:
        conn.execute(
            "update public.memberships set status='active', joined_at=now()"
            " where team_id=%s and user_id=%s",
            (TEAM_A, A1),
        )
    assert admin.execute(
        "select archived_at from public.teams where id=%s", (TEAM_A,)
    ).fetchone()[0] is None


# ---------------------------------------------------------------------------
# The identity guard, now that a worker role can write here too
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("column,value", [("team_id", TEAM_B), ("user_id", B1)])
def test_a_membership_cannot_be_moved_between_teams_or_people(seeded, column, value):
    """trg_membership_identity returned early for worker roles (auth.uid()
    null), which was safe only while no worker could UPDATE memberships. The
    consent-routed departure gives comrade_executor exactly that, and its
    policy pins team_id and status but says nothing about user_id — so the
    guard has to cover every actor, not just humans."""
    with pytest.raises(psycopg.Error, match="cannot be moved between teams or members"):
        with team_session(Role.EXECUTOR, TEAM_A) as conn:
            conn.execute(
                f"update public.memberships set {column}=%s, status='left'"
                " where team_id=%s and user_id=%s",
                (value, TEAM_A, A2),
            )


# ---------------------------------------------------------------------------
# Succession
# ---------------------------------------------------------------------------

def test_leadership_passes_on_when_the_leader_leaves(seeded, admin):
    """Otherwise D4 ships a trap.

    The leader is the only one who can invite (server/invites.py) and rename
    (au_teams_update). Add a leave button without succession and the moment a
    founding leader walks out, a team with three remaining members can never
    grow again — and nothing in the UI would say why.

    Passing the role on keeps the authority shape exactly as it was, which is
    a smaller change than widening who may invite.
    """
    _leave(A1, TEAM_A)  # A1 is Team A's leader
    assert admin.execute(
        "select role from public.memberships where team_id=%s and user_id=%s",
        (TEAM_A, A2),
    ).fetchone()[0] == "leader"


A3 = "a3a3a3a3-0000-0000-0000-000000000003"


def test_succession_picks_the_longest_standing_member(seeded, admin):
    """With one member left the choice is vacuous, so this needs a third.

    A2 joined a month ago, A3 today. When the leader goes, the team should
    land with the person who has been there longest — not with whoever the
    planner happened to return first.
    """
    admin.execute(
        "insert into auth.users (instance_id, id, aud, role, email,"
        " encrypted_password, created_at, updated_at) values"
        " ('00000000-0000-0000-0000-000000000000', %s, 'authenticated',"
        " 'authenticated', 'a3@test.dev', '', now(), now())"
        " on conflict (id) do nothing",
        (A3,),
    )
    try:
        admin.execute(
            "insert into public.profiles (id, display_name) values (%s,'A3')"
            " on conflict (id) do nothing",
            (A3,),
        )
        admin.execute(
            "update public.memberships set joined_at = now() - interval '30 days'"
            " where team_id=%s and user_id=%s",
            (TEAM_A, A2),
        )
        admin.execute(
            "insert into public.memberships (team_id, user_id, role, status,"
            " joined_at) values (%s,%s,'member','active', now())",
            (TEAM_A, A3),
        )
        _leave(A1, TEAM_A)
        leaders = [
            str(r[0])
            for r in admin.execute(
                "select user_id from public.memberships"
                " where team_id=%s and role='leader' and status='active'",
                (TEAM_A,),
            ).fetchall()
        ]
        assert leaders == [A2], "the newest arrival was handed the team"
    finally:
        admin.execute("delete from auth.users where id=%s", (A3,))


def test_succession_does_not_hand_anyone_else_the_promote_button(seeded):
    """The guard the succession exemption punches through must still hold for
    people. A2 is an ordinary member and stays one."""
    with pytest.raises(psycopg.Error):
        with as_user(A2) as conn:
            conn.execute(
                "update public.memberships set role='leader'"
                " where team_id=%s and user_id=%s",
                (TEAM_A, A2),
            )
