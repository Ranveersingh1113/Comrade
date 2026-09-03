"""Scenario setup: four people, a team, a repository.

Everything goes through the real paths — GoTrue signup, RLS-governed inserts as
each member, the same endpoints the browser calls. Nothing is seeded as admin
except reading back for the assessment, because a simulation that bypasses the
authorisation layer is not testing the system that would ship.
"""
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.config import settings  # noqa: E402
from shared.db import user_session  # noqa: E402

API = "http://localhost:8000"
STATE = Path(__file__).resolve().parent / "state.json"

PEOPLE = [
    ("priya", "Priya Sharma"),
    ("marcus", "Marcus Lee"),
    ("aisha", "Aisha Khan"),
    ("tom", "Tom Alvarez"),
]
PASSWORD = "sim-password-1"


def _auth(path: str, body: dict, *, tolerate: tuple[str, ...] = ()) -> dict:
    resp = httpx.post(
        f"{settings.supabase_url.rstrip('/')}/auth/v1/{path}",
        headers={"apikey": settings.supabase_anon_key,
                 "Content-Type": "application/json"},
        json=body, timeout=30,
    )
    if resp.status_code >= 400:
        if any(t in resp.text for t in tolerate):
            return {}
        raise SystemExit(f"{path} failed: {resp.status_code} {resp.text[:200]}")
    return resp.json()


def sign_up(tag: str, display: str) -> dict:
    email = f"sim-{tag}@test.dev"
    # Idempotent: re-running the scenario should not need a database reset.
    _auth("signup", {"email": email, "password": PASSWORD,
                     "data": {"display_name": display}},
          tolerate=("user_already_exists",))
    token = _auth("token?grant_type=password",
                  {"email": email, "password": PASSWORD})
    user_id = token["user"]["id"]
    # The profile row the app expects. Written AS THE USER so the same RLS a
    # browser faces applies.
    with user_session(user_id) as conn:
        conn.execute(
            "insert into public.profiles (id, display_name, email)"
            " values (%s,%s,%s) on conflict (id) do update"
            "   set display_name = excluded.display_name",
            (user_id, display, email),
        )
    return {"tag": tag, "name": display, "email": email,
            "id": user_id, "token": token["access_token"]}


def main() -> None:
    people = [sign_up(tag, name) for tag, name in PEOPLE]
    lead = people[0]

    # A previous run may have left a team; the scenario wants a fresh one.
    import psycopg

    admin = psycopg.connect(settings.comrade_db_url_admin)
    admin.autocommit = True
    admin.execute("delete from public.teams where name = 'Snake'")
    admin.close()

    with user_session(lead["id"]) as conn:
        # created_by must equal auth.uid(): au_teams_insert checks it, so a
        # team cannot be founded on somebody else's behalf.
        team_id = str(conn.execute(
            "insert into public.teams (name, created_by) values (%s,%s)"
            " returning id",
            ("Snake", lead["id"]),
        ).fetchone()[0])
        # status='active', not the 'invited' default. is_team_leader requires
        # active, so a founder who omits it is not a leader by any policy's
        # definition and cannot invite anyone — the first thing this
        # simulation tripped over.
        conn.execute(
            "insert into public.memberships"
            " (team_id, user_id, role, status, joined_at)"
            " values (%s,%s,'leader','active', now())",
            (team_id, lead["id"]),
        )

    # THE JOIN IS TWO STEPS, and both are simulated because "joining" is part
    # of what is being tested. The lead invites (status 'invited'); each person
    # accepts by updating their OWN row. A leader cannot mark someone active on
    # their behalf, which is what stops a team assembling members who never
    # agreed to be in it.
    for member in people[1:]:
        with user_session(lead["id"]) as conn:
            conn.execute(
                "insert into public.memberships (team_id, user_id, role, status)"
                " values (%s,%s,'member','invited')",
                (team_id, member["id"]),
            )
        with user_session(member["id"]) as conn:
            accepted = conn.execute(
                "update public.memberships set status='active', joined_at=now()"
                " where team_id=%s and user_id=%s",
                (team_id, member["id"]),
            ).rowcount
            if accepted != 1:
                raise SystemExit(f"{member['name']} could not accept the invite")

    STATE.write_text(json.dumps(
        {"team_id": team_id, "people": people}, indent=2), encoding="utf-8")
    print(f"team Snake  {team_id}")
    for p in people:
        print(f"  {p['name']:14} {p['id']}")


if __name__ == "__main__":
    main()
