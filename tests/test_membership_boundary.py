"""You cannot walk into a team by knowing its id.

🔴 Found 2026-08-31 while scoping team lifecycle. `au_memberships_insert`
carried `with check ((user_id = auth.uid()) OR is_team_leader(team_id))`. The
first branch was there so a member can found a team — create it, then insert
themselves as leader — but as written it let ANY authenticated user insert
themselves into ANY team, as an active member, given only its id.

Team ids are UUIDs, so not guessable. They are also in every URL
(`/t/<teamId>/room`), in localStorage, and in any link or screenshot a member
shares. Every helper that gates the product — is_team_member, is_team_leader,
shares_team — trusts an active membership row, so one insert buys the room,
the wiki, tasks, documents and every other member's contribution record.

This was more reachable than either private-thread leak closed earlier: those
needed a database role, this needed a logged-in account and a URL.
"""
import psycopg
import pytest

from shared.config import settings
from tests._seed import A1, B1, TEAM_A, as_user


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def test_a_stranger_cannot_join_a_team_by_id(seeded, admin):
    """The hole. B1 belongs to TEAM_B and knows TEAM_A's id, nothing more."""
    with pytest.raises(psycopg.Error):
        with as_user(B1) as conn:
            conn.execute(
                "insert into public.memberships (team_id, user_id, role, status)"
                " values (%s,%s,'member','active')",
                (TEAM_A, B1),
            )
    assert admin.execute(
        "select count(*) from public.memberships where team_id=%s and user_id=%s",
        (TEAM_A, B1),
    ).fetchone()[0] == 0


def test_a_stranger_cannot_join_as_leader_either(seeded, admin):
    with pytest.raises(psycopg.Error):
        with as_user(B1) as conn:
            conn.execute(
                "insert into public.memberships (team_id, user_id, role, status)"
                " values (%s,%s,'leader','active')",
                (TEAM_A, B1),
            )


def test_founding_a_team_still_works(seeded, admin):
    """The legitimate use of the self-insert branch, which must survive.

    frontend/src/screens/TeamGate.tsx: insert the team, then insert yourself
    as its leader. At that instant the team has no members, so nothing else
    could authorise the row.
    """
    with as_user(B1, commit=True) as conn:
        team_id = conn.execute(
            "insert into public.teams (name, created_by) values ('B1 new team',%s)"
            " returning id",
            (B1,),
        ).fetchone()[0]
        conn.execute(
            "insert into public.memberships (team_id, user_id, role, status,"
            " joined_at) values (%s,%s,'leader','active', now())",
            (team_id, B1),
        )
    assert admin.execute(
        "select count(*) from public.memberships where team_id=%s", (team_id,)
    ).fetchone()[0] == 1
    admin.execute("delete from public.teams where id=%s", (team_id,))


def test_you_cannot_found_your_way_into_someone_elses_empty_team(seeded, admin):
    """An empty team is not an open door — only its creator may seed it."""
    orphan = admin.execute(
        "insert into public.teams (name, created_by) values ('orphan',%s)"
        " returning id",
        (A1,),
    ).fetchone()[0]
    try:
        with pytest.raises(psycopg.Error):
            with as_user(B1) as conn:
                conn.execute(
                    "insert into public.memberships (team_id, user_id, role,"
                    " status) values (%s,%s,'leader','active')",
                    (orphan, B1),
                )
    finally:
        admin.execute("delete from public.teams where id=%s", (orphan,))


def test_a_leader_may_still_add_a_member(seeded, admin):
    """server/invites.py writes the membership row as the leader, under this
    same policy — the invite path must not break."""
    with as_user(A1, commit=True) as conn:
        conn.execute(
            "insert into public.memberships (team_id, user_id, role, status)"
            " values (%s,%s,'member','invited')",
            (TEAM_A, B1),
        )
    assert admin.execute(
        "select status from public.memberships where team_id=%s and user_id=%s",
        (TEAM_A, B1),
    ).fetchone()[0] == "invited"


# ---------------------------------------------------------------------------
# 🔴 Found while fixing the above: nobody could create a team through the UI.
# ---------------------------------------------------------------------------

def test_a_member_can_create_a_team_the_way_the_app_does(seeded, admin):
    """`INSERT ... RETURNING`, which is what the client actually issues.

    frontend/src/screens/TeamGate.tsx does
    `.insert({...}).select().single()`, and PostgREST turns that into
    INSERT ... RETURNING. RETURNING makes the new row pass the SELECT policy
    as well as the INSERT one — and au_teams_select was `is_team_member(id)`,
    which the creator does not satisfy yet: their membership row is the NEXT
    statement.

    So a bare INSERT succeeded and the app's INSERT failed, which is why no
    test caught it: nothing had ever created a team the way the product does.
    Team creation is the first thing a new user does.
    """
    with as_user(B1, commit=True) as conn:
        team_id = conn.execute(
            "insert into public.teams (name, created_by) values ('via the app',%s)"
            " returning id",
            (B1,),
        ).fetchone()[0]
    try:
        assert team_id is not None
    finally:
        admin.execute("delete from public.teams where id=%s", (team_id,))


def test_creating_a_team_does_not_reveal_other_teams(seeded, admin):
    """Widening the select policy must not widen it past the creator."""
    with as_user(B1) as conn:
        visible = conn.execute(
            "select count(*) from public.teams where id=%s", (TEAM_A,)
        ).fetchone()[0]
    assert visible == 0, "B1 is not in TEAM_A and did not create it"
