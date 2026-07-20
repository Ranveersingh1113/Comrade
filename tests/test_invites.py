"""Invite-by-email: profile auto-creation, leader gate, and the real GoTrue flow (local stack)."""
import uuid

import psycopg
import pytest

from server.invites import invite_member
from shared.config import settings
from tests._seed import A1, A2, TEAM_A


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def test_new_auth_user_gets_a_profile_automatically(seeded):
    """The trigger closes the FK gap that blocked inviting unknown emails."""
    uid = str(uuid.uuid4())
    conn = _admin()
    try:
        conn.execute(
            "insert into auth.users (instance_id, id, aud, role, email,"
            " encrypted_password, created_at, updated_at) values"
            " ('00000000-0000-0000-0000-000000000000', %s, 'authenticated',"
            " 'authenticated', %s, '', now(), now())",
            (uid, f"fresh-{uid[:8]}@test.dev"),
        )
        row = conn.execute(
            "select display_name from public.profiles where id=%s", (uid,)
        ).fetchone()
        assert row == (f"fresh-{uid[:8]}",)  # email local part
    finally:
        conn.execute("delete from auth.users where id=%s", (uid,))
        conn.close()


def test_member_lookup_fn_is_closed_to_authenticated(seeded):
    from tests._seed import as_user

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with as_user(A2) as conn:
            conn.execute("select public.user_id_by_email('a1@test.dev')")


def test_non_leader_cannot_invite(seeded):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        invite_member(TEAM_A, A2, "someone@test.dev")  # A2 is a member
    assert exc.value.status_code == 403


def test_leader_invites_a_brand_new_email_end_to_end(seeded):
    """Real GoTrue: auth user created, profile materialised, membership invited."""
    email = f"pilot-{uuid.uuid4().hex[:10]}@test.dev"
    out = invite_member(TEAM_A, A1, email)
    assert out["status"] == "invited"

    conn = _admin()
    try:
        prof, memb = conn.execute(
            "select (select count(*) from public.profiles where id=%(u)s),"
            " (select status from public.memberships"
            "  where team_id=%(t)s and user_id=%(u)s)",
            {"u": out["user_id"], "t": TEAM_A},
        ).fetchone()
        assert (prof, memb) == (1, "invited")
    finally:
        # auth cascade cleans profile; membership dies with it
        conn.execute("delete from auth.users where id=%s", (out["user_id"],))
        conn.close()


def test_inviting_an_existing_account_resolves_instead_of_failing(seeded):
    """GoTrue only dedupes accounts IT registered (identity rows), so the
    existing account must come from a real signup, not the DB seed."""
    import httpx

    email = f"veteran-{uuid.uuid4().hex[:10]}@test.dev"
    base = settings.supabase_url.rstrip("/")
    key = settings.supabase_secret_key
    signup = httpx.post(
        f"{base}/auth/v1/signup",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        json={"email": email, "password": "a-strong-enough-pw-1"},
        timeout=10.0,
    )
    assert signup.status_code == 200
    existing_id = signup.json().get("id") or signup.json()["user"]["id"]

    try:
        out = invite_member(TEAM_A, A1, email)
        assert out["user_id"] == existing_id
        assert out["status"] == "invited"
    finally:
        conn = _admin()
        try:
            conn.execute("delete from auth.users where id=%s", (existing_id,))
        finally:
            conn.close()
