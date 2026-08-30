"""Invite a member by email, including people with no account yet.

The RLS path (leader inserts a membership row) only works when the invitee
already has a profiles row — impossible for someone who has never logged in.
This bridges the gap: GoTrue creates (or identifies) the auth user, the
auth.users trigger materialises the profile, and the membership row is then
written AS THE LEADER under their own RLS policy — the API adds no authority
of its own beyond the auth-plane call.

The GoTrue secret key never leaves this module and is never exposed to the
browser.
"""
import httpx
from fastapi import HTTPException, status

from shared.config import settings
from shared.db import Role, connect, user_session


def _auth_headers() -> dict[str, str]:
    key = settings.supabase_secret_key
    return {"apikey": key, "Authorization": f"Bearer {key}"}


def _require_leader(user_id: str, team_id: str) -> None:
    """Only the leader invites (administrative action, per the ownership model)."""
    with user_session(user_id) as conn:
        row = conn.execute(
            "select 1 from public.memberships where team_id=%s and user_id=%s"
            " and role='leader' and status='active'",
            (team_id, user_id),
        ).fetchone()
    if row is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "only the team leader can invite"
        )


def _invite_or_resolve_user(email: str) -> str:
    """GoTrue-invite the email; if already registered, resolve to the user id."""
    if not settings.supabase_secret_key:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "SUPABASE_SECRET_KEY is not configured; invites unavailable",
        )
    base = settings.supabase_url.rstrip("/")
    resp = httpx.post(
        f"{base}/auth/v1/invite", headers=_auth_headers(),
        json={"email": email}, timeout=10.0,
    )
    if resp.status_code == 200:
        return str(resp.json()["id"])

    if resp.status_code == 429:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "invite emails are rate-limited right now — try again shortly",
        )

    body = resp.text.lower()
    if "already" in body or "exists" in body or resp.status_code == 422:
        # Registered before — resolve via the agent-only definer function
        # (no team scoping needed: it reads auth.users, not team data).
        with connect(Role.AGENT) as conn:
            row = conn.execute(
                "select public.user_id_by_email(%s)", (email,)
            ).fetchone()
        if row and row[0]:
            return str(row[0])
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "could not resolve existing account"
        )
    raise HTTPException(
        status.HTTP_502_BAD_GATEWAY, f"invite failed: {resp.status_code}"
    )


def invite_member(team_id: str, leader_id: str, email: str) -> dict:
    """Leader invites an email address; returns the created membership state."""
    _require_leader(leader_id, team_id)
    invitee_id = _invite_or_resolve_user(email)

    # Written as the leader — au_memberships_insert's leader branch authorises.
    with user_session(leader_id) as conn:
        row = conn.execute(
            "insert into public.memberships (team_id, user_id, role, status)"
            " values (%s,%s,'member','invited')"
            " on conflict (team_id, user_id) do nothing"
            " returning id",
            (team_id, invitee_id),
        ).fetchone()
    if row is None:
        return {"status": "already_member", "user_id": invitee_id}
    return {
        "status": "invited",
        "user_id": invitee_id,
        "membership_id": str(row[0]),
    }
