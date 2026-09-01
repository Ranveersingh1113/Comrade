"""Members cannot write memory. They can argue with it.

findings §6.3-6 names the gap: *"no conflict state — disagreement collapses to
`revise`, the losing side vanishes."* Consolidation resolves every conflict by
picking a winner and marking the loser inactive, and the reasoning that
produced the disagreement is nowhere. The wiki records what the team currently
believes and never why anyone objected.

A comment is where the losing side stays visible. It is also the members' half
of the wiki, and the reason it must be its OWN table rather than a column on
memory_versions: memory_* is comrade_pipeline's alone (§6.0), and it should
stay that way. Comments are member-written by construction, so they live
somewhere members may write.

THE ANCHOR IS THE ENTRY, NOT THE VERSION
------------------------------------------
This is the whole design decision. A memory_entry is the durable identity of a
fact; a memory_version is one statement of it, and consolidation replaces those
routinely. Anchoring a comment to the version it was written against would
orphan every discussion on the next compile — the objection would detach from
the fact it was about at exactly the moment the fact changed, which is the one
moment it matters.
"""
import psycopg
import pytest

from shared.config import settings
from tests._seed import A1, A2, B1, ENTRY_A, TEAM_A, VER_A, as_user


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def _comment(uid, body, entry_id=ENTRY_A, team_id=TEAM_A):
    with as_user(uid, commit=True) as conn:
        return conn.execute(
            "insert into public.memory_comments (entry_id, team_id, author_id, body)"
            " values (%s,%s,%s,%s) returning id",
            (entry_id, team_id, uid, body),
        ).fetchone()[0]


# ---------------------------------------------------------------------------
# The point of the feature
# ---------------------------------------------------------------------------

def test_a_comment_survives_the_fact_being_revised(seeded, admin):
    """§6.3-6, directly.

    A2 objects to a fact. Consolidation then revises it — the version A2 was
    arguing with goes inactive and a new one takes its place. The objection has
    to still be attached, because a disagreement about a fact is about the
    FACT, not about one phrasing of it. Anchored to the version, this comment
    would now point at something the wiki no longer says.
    """
    comment_id = _comment(A2, "this was true in May, not now")

    admin.execute(
        "update public.memory_versions set is_active=false, valid_until=now()"
        " where id=%s",
        (VER_A,),
    )
    admin.execute(
        "insert into public.memory_versions (entry_id, team_id, fact, change_type)"
        " values (%s,%s,'deadline is Monday','revised')",
        (ENTRY_A, TEAM_A),
    )

    with as_user(A1) as conn:
        rows = conn.execute(
            "select body from public.memory_comments where entry_id=%s", (ENTRY_A,)
        ).fetchall()
    assert [r[0] for r in rows] == ["this was true in May, not now"]


def test_the_whole_team_can_read_the_disagreement(seeded):
    """A comment nobody else sees is a private note, and §6.3-6 is about the
    losing side staying visible to the team."""
    _comment(A2, "I think this is wrong")
    with as_user(A1) as conn:
        assert conn.execute(
            "select count(*) from public.memory_comments where entry_id=%s",
            (ENTRY_A,),
        ).fetchone()[0] == 1


def test_a_member_comments_as_themselves(seeded):
    """Authorship is the point — an anonymous objection is not a position
    anyone holds."""
    with pytest.raises(psycopg.Error):
        with as_user(A2) as conn:
            conn.execute(
                "insert into public.memory_comments (entry_id, team_id, author_id,"
                " body) values (%s,%s,%s,'signed by someone else')",
                (ENTRY_A, TEAM_A, A1),
            )


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------

def test_another_team_cannot_read_or_write_them(seeded):
    """A comment quotes the fact it argues with, so it leaks the wiki if it
    leaks at all."""
    _comment(A2, "an internal disagreement")
    with as_user(B1) as conn:
        assert conn.execute(
            "select count(*) from public.memory_comments"
        ).fetchone()[0] == 0
    with pytest.raises(psycopg.Error):
        with as_user(B1) as conn:
            conn.execute(
                "insert into public.memory_comments (entry_id, team_id, author_id,"
                " body) values (%s,%s,%s,'butting in')",
                (ENTRY_A, TEAM_A, B1),
            )


def test_a_comment_cannot_be_filed_against_another_team_s_entry(seeded, admin):
    """team_id and entry_id arrive together and could disagree. If nothing
    checks, a member could attach a comment to a fact in a team they belong to
    while naming an entry from one they do not — and read it back."""
    from tests._seed import TEAM_B

    other_entry = admin.execute(
        "insert into public.memory_entries (team_id) values (%s) returning id",
        (TEAM_B,),
    ).fetchone()[0]
    with pytest.raises(psycopg.Error):
        with as_user(A1) as conn:
            conn.execute(
                "insert into public.memory_comments (entry_id, team_id, author_id,"
                " body) values (%s,%s,%s,'reaching across')",
                (other_entry, TEAM_A, A1),
            )


def test_members_still_cannot_write_memory_itself(seeded):
    """The constraint this feature exists inside. If commenting were a column
    on memory_versions, members would need write access to the table the
    compiler owns alone (§6.0)."""
    with pytest.raises(psycopg.Error):
        with as_user(A1) as conn:
            conn.execute(
                "insert into public.memory_versions (entry_id, team_id, fact,"
                " change_type) values (%s,%s,'I disagree','added')",
                (ENTRY_A, TEAM_A),
            )


# ---------------------------------------------------------------------------
# Editing your own words
# ---------------------------------------------------------------------------

def test_an_author_can_withdraw_their_own_comment(seeded, admin):
    comment_id = _comment(A2, "on reflection this was unfair")
    with as_user(A2, commit=True) as conn:
        conn.execute("delete from public.memory_comments where id=%s", (comment_id,))
    assert admin.execute(
        "select count(*) from public.memory_comments where id=%s", (comment_id,)
    ).fetchone()[0] == 0


def test_nobody_else_can_withdraw_it_for_them(seeded, admin):
    """Including the team leader. An objection a teammate can delete is not an
    objection — it is a suggestion the majority may erase, which is precisely
    the collapse §6.3-6 describes, moved one level up.
    """
    comment_id = _comment(A2, "I still think this is wrong")
    with as_user(A1, commit=True) as conn:  # A1 is Team A's leader
        conn.execute("delete from public.memory_comments where id=%s", (comment_id,))
    assert admin.execute(
        "select count(*) from public.memory_comments where id=%s", (comment_id,)
    ).fetchone()[0] == 1


def test_a_comment_goes_with_its_entry(seeded, admin):
    """Cascade, not orphan: an entry that is deleted outright takes its
    discussion with it, because a comment about nothing is unreadable."""
    entry = admin.execute(
        "insert into public.memory_entries (team_id) values (%s) returning id",
        (TEAM_A,),
    ).fetchone()[0]
    _comment(A1, "about the doomed entry", entry_id=entry)
    admin.execute("delete from public.memory_entries where id=%s", (entry,))
    assert admin.execute(
        "select count(*) from public.memory_comments where entry_id=%s", (entry,)
    ).fetchone()[0] == 0


def test_an_empty_comment_is_refused(seeded):
    """A blank row renders as an author and a date attached to nothing, which
    reads as a redaction rather than an accident."""
    with pytest.raises(psycopg.Error):
        with as_user(A1) as conn:
            conn.execute(
                "insert into public.memory_comments (entry_id, team_id, author_id,"
                " body) values (%s,%s,%s,'   ')",
                (ENTRY_A, TEAM_A, A1),
            )
