"""Publishing private work into the room, without laundering who wrote it.

findings §13.4, owner decision 2026-08-17. A member working with Comrade in
their private thread gets to put that work in front of the team — and the shape
chosen is deliberately the boring one: **the member's own message**, with a
marker saying they had help.

    attribution   sender_kind='user', sender_id = auth.uid()
    provenance    messages.ai_assisted, rendered as "drafted with Comrade"
    transport     a plain insert from the browser
    consent       none — a person composing and sending their own message is
                  not a gated action

The alternative shapes are worse in ways worth naming. An AI-authored message
in the room breaks §13's rule that the agent never originates group content. A
consent card for "may I say this thing I wrote" makes a person ask permission
to speak. Attribution to Comrade lets a member disown work published under
their own name.

This file also checks the claim that made the feature cheap: that no policy
change was needed. That is exactly the kind of assertion which is cheap to make
and expensive to be wrong about.
"""
import psycopg
import pytest

from shared.config import settings
from tests._seed import general_thread, personal_thread, A1, A2, TEAM_A, as_user


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def test_a_member_publishes_it_as_themselves(seeded, admin):
    """The whole feature, and it needed no new policy.

    au_messages_insert already requires sender_kind='user' and
    sender_id = auth.uid(), and RLS gates ROWS not columns — so a member may
    set this on their own insert with nothing added anywhere.
    """
    with as_user(A1, commit=True) as conn:
        conn.execute(
            "insert into public.messages (team_id, thread_id, sender_kind,"
            " sender_id, body, ai_assisted) values (%s,%s,'user',%s,"
            " 'Here is the summary I put together.', true)",
            (TEAM_A, general_thread(conn, TEAM_A), A1),
        )
    row = admin.execute(
        "select sender_kind, sender_id, ai_assisted from public.messages"
        " where team_id=%s and ai_assisted order by created_at desc limit 1",
        (TEAM_A,),
    ).fetchone()
    assert row[0] == "user"
    assert str(row[1]) == A1, "the member owns it — attribution is not Comrade's"
    assert row[2] is True


def test_an_ordinary_message_is_not_marked(seeded, admin):
    """Default false: the marker means something only if most messages lack
    it, and a backfill that guessed would put it on every message ever sent."""
    with as_user(A1, commit=True) as conn:
        conn.execute(
            "insert into public.messages (team_id, thread_id, sender_kind,"
            " sender_id, body) values (%s,%s,'user',%s,'morning all')",
            (TEAM_A, general_thread(conn, TEAM_A), A1),
        )
    assert admin.execute(
        "select ai_assisted from public.messages where body='morning all'"
    ).fetchone()[0] is False


def test_an_ai_message_cannot_claim_to_be_ai_assisted(seeded, admin):
    """Nonsense, and the column is otherwise free for anything writing a
    message to set. The marker means "a person wrote this WITH help"; on
    Comrade's own output it is either redundant or a lie about who is speaking.
    """
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute(
            "insert into public.messages (team_id, thread_id, sender_kind,"
            " sender_id, body, ai_assisted) values (%s,%s,'ai',null,"
            " 'I wrote this myself, with my own help', true)",
            (TEAM_A, general_thread(admin, TEAM_A)),
        )


def test_a_member_still_cannot_publish_under_someone_else_s_name(seeded):
    """The marker must not become a loophole. Everything au_messages_insert
    enforced before still holds — this column rides on that policy, it does not
    replace any part of it."""
    with pytest.raises(psycopg.Error):
        with as_user(A1) as conn:
            conn.execute(
                "insert into public.messages (team_id, thread_id, sender_kind,"
                " sender_id, body, ai_assisted) values (%s,%s,'user',%s,"
                " 'A2 definitely said this', true)",
                (TEAM_A, general_thread(conn, TEAM_A), A2),
            )


def test_publishing_is_a_new_message_not_a_move(seeded, admin):
    """Q8: the private original stays private.

    Publishing composes a NEW group message; it does not reclassify the AI's
    private reply. If it moved the row, the member's private thread would
    silently lose part of its history, and the thing published would carry
    sender_kind='ai' — which §13 forbids in the room.
    """
    private_id = admin.execute(
        "insert into public.messages (team_id, thread_id,"
        " sender_kind, sender_id, body) values (%s,%s,'ai',null,"
        " 'Here is a draft of the summary.') returning id",
        (TEAM_A, personal_thread(admin, TEAM_A, A1)),
    ).fetchone()[0]

    with as_user(A1, commit=True) as conn:
        conn.execute(
            "insert into public.messages (team_id, thread_id, sender_kind,"
            " sender_id, body, ai_assisted) values (%s,%s,'user',%s,"
            " 'Here is a draft of the summary.', true)",
            (TEAM_A, general_thread(conn, TEAM_A), A1),
        )

    # "still private" is a property of the THREAD now, not a column on the
    # message — the publish must not have moved the original anywhere.
    still_private = admin.execute(
        "select t.visibility, m.sender_kind from public.messages m"
        " join public.threads t on t.id = m.thread_id where m.id=%s",
        (private_id,),
    ).fetchone()
    assert still_private == ("restricted", "ai")


def test_a_teammate_cannot_read_the_private_original(seeded, admin):
    """The boundary publishing must not weaken. A2 sees what A1 chose to
    publish and nothing else from that thread."""
    admin.execute(
        "insert into public.messages (team_id, thread_id,"
        " sender_kind, sender_id, body) values (%s,%s,'ai',null,"
        " 'the part A1 decided not to share') returning id",
        (TEAM_A, personal_thread(admin, TEAM_A, A1)),
    )
    with as_user(A1, commit=True) as conn:
        conn.execute(
            "insert into public.messages (team_id, thread_id, sender_kind,"
            " sender_id, body, ai_assisted) values (%s,%s,'user',%s,"
            " 'the part A1 did share', true)",
            (TEAM_A, general_thread(conn, TEAM_A), A1),
        )
    with as_user(A2) as conn:
        bodies = [
            r[0] for r in conn.execute(
                "select body from public.messages where team_id=%s", (TEAM_A,)
            ).fetchall()
        ]
    assert "the part A1 did share" in bodies
    assert "the part A1 decided not to share" not in bodies
