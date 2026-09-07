"""Shared seed data + helpers for DB tests (RLS + triggers)."""
import json
from contextlib import contextmanager

import psycopg

from shared.config import settings

# fixed ids (match tests/rls_isolation_test.sql)
TEAM_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
TEAM_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
A1 = "a1a1a1a1-0000-0000-0000-000000000001"
A2 = "a2a2a2a2-0000-0000-0000-000000000002"
B1 = "b1b1b1b1-0000-0000-0000-000000000001"
B2 = "b2b2b2b2-0000-0000-0000-000000000002"
ENTRY_A = "e0000000-0000-0000-0000-0000000000e1"
VER_A = "f0000000-0000-0000-0000-0000000000f1"

_USERS = [
    (A1, "a1@test.dev"), (A2, "a2@test.dev"),
    (B1, "b1@test.dev"), (B2, "b2@test.dev"),
]


def seed(cur):
    for uid, email in _USERS:
        cur.execute(
            "insert into auth.users (instance_id, id, aud, role, email,"
            " encrypted_password, created_at, updated_at) values"
            " ('00000000-0000-0000-0000-000000000000', %s, 'authenticated',"
            " 'authenticated', %s, '', now(), now())",
            (uid, email),
        )
    cur.executemany(
        # the auth.users trigger may have created the profile already — upsert
        "insert into public.profiles (id, display_name) values (%s, %s)"
        " on conflict (id) do update set display_name = excluded.display_name",
        [(A1, "A1"), (A2, "A2"), (B1, "B1"), (B2, "B2")],
    )
    cur.executemany(
        "insert into public.teams (id, name, created_by) values (%s, %s, %s)",
        [(TEAM_A, "Team A", A1), (TEAM_B, "Team B", B1)],
    )
    cur.executemany(
        "insert into public.memberships (team_id, user_id, role, status)"
        " values (%s, %s, %s, 'active')",
        [(TEAM_A, A1, "leader"), (TEAM_A, A2, "member"),
         (TEAM_B, B1, "leader"), (TEAM_B, B2, "member")],
    )
    private_thread = cur.execute(
        "insert into public.threads (team_id, title, visibility, kind, owner_id, created_by)"
        " values (%s, 'Private', 'restricted', 'discussion', %s, %s) returning id",
        (TEAM_A, A1, A1),
    ).fetchone()[0]
    cur.execute(
        "insert into public.thread_participants (thread_id, team_id, user_id, added_by)"
        " values (%s, %s, %s, %s)",
        (private_thread, TEAM_A, A1, A1),
    )
    cur.execute(
        "insert into public.messages (team_id, thread_id, sender_kind, sender_id, body)"
        " values (%s, %s, 'user', %s, 'A1 private note')",
        (TEAM_A, private_thread, A1),
    )
    general_thread = cur.execute(
        "select id from public.threads where team_id=%s and title='General'",
        (TEAM_A,),
    ).fetchone()[0]
    cur.execute(
        # Backdated a minute: capture stays CAPTURE_LAG_SECONDS behind the
        # clock (T16), so a message written this instant is not eligible yet
        # and every threshold test would be one short. Still far inside
        # MAX_CAPTURE_AGE, so the age trigger stays out of those tests.
        "insert into public.messages (team_id, thread_id, sender_kind, sender_id,"
        " body, created_at)"
        " values (%s, %s, 'user', %s, 'hello team A', now() - interval '1 minute')",
        (TEAM_A, general_thread, A2),
    )
    cur.execute(
        "insert into public.memory_entries (id, team_id) values (%s, %s)",
        (ENTRY_A, TEAM_A),
    )
    cur.execute(
        "insert into public.memory_versions (id, entry_id, team_id, fact,"
        " change_type) values (%s, %s, %s, 'deadline is Friday', 'added')",
        (VER_A, ENTRY_A, TEAM_A),
    )
    cur.execute(
        "insert into public.jobs (team_id, job_type) values (%s, 'parse_document')",
        (TEAM_A,),
    )


def cleanup(cur):
    cur.execute("delete from public.teams where id in (%s, %s)", (TEAM_A, TEAM_B))
    cur.execute(
        "delete from auth.users where id in (%s, %s, %s, %s)", (A1, A2, B1, B2)
    )


@contextmanager
def as_user(uid, commit=False):
    """Impersonate an end user (role authenticated + JWT claims).

    Rolls back by default; pass commit=True when a later session must see the
    change (the `seeded` fixture cleans up either way).
    """
    conn = psycopg.connect(settings.comrade_db_url_admin)
    try:
        conn.execute("set role authenticated")
        conn.execute(
            "select set_config('request.jwt.claims', %s, false)",
            (json.dumps({"sub": uid, "role": "authenticated"}),),
        )
        yield conn
        conn.commit() if commit else conn.rollback()
    finally:
        conn.close()


def count(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


# ---------------------------------------------------------------------------
# Naming a thread the way tests think about one
# ---------------------------------------------------------------------------
# The contract migration (20260904100000) dropped messages.thread_type and
# messages.thread_owner_id: a message now names its thread by id and nothing
# else. Tests still reason in terms of "the room" and "that member's private
# thread", so these translate — rather than every test file growing its own
# copy of the same two selects, which is what it was doing.
#
# Both are LOOKUPS, deliberately. A get-or-create would paper over a thread
# that should exist and does not, which is a real failure worth seeing: the
# backfill only made a personal thread for owners who already had a private
# message, so a member who never wrote one has none until something gives them
# one.

def general_thread(conn, team_id) -> str:
    """The team-visible thread every team gets from the backfill."""
    row = conn.execute(
        "select id from public.threads where team_id=%s and title='General'",
        (team_id,),
    ).fetchone()
    assert row is not None, f"team {team_id} has no General thread"
    return str(row[0])


def personal_thread(conn, team_id, owner_id) -> str:
    """That member's restricted thread. Fails loudly when they have none."""
    row = conn.execute(
        "select id from public.threads where team_id=%s and owner_id=%s",
        (team_id, owner_id),
    ).fetchone()
    assert row is not None, (
        f"member {owner_id} has no personal thread in team {team_id}."
        " The backfill only created one per owner FOUND IN MESSAGES, so a"
        " member who never wrote a private message has none — create it in"
        " the test rather than relying on the seed."
    )
    return str(row[0])
