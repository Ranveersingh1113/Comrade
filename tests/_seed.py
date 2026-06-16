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
        "insert into public.profiles (id, display_name) values (%s, %s)",
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
    cur.execute(
        "insert into public.messages (team_id, thread_type, thread_owner_id,"
        " sender_kind, sender_id, body) values"
        " (%s, 'private', %s, 'user', %s, 'A1 private note')",
        (TEAM_A, A1, A1),
    )
    cur.execute(
        "insert into public.messages (team_id, thread_type, sender_kind,"
        " sender_id, body) values (%s, 'group', 'user', %s, 'hello team A')",
        (TEAM_A, A2),
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
