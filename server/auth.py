"""Request authentication for the HTTP surface.

Identity comes from a Supabase-issued JWT and nothing else. The previous
dev-only contract read `requester_id` from the request body, which let any
caller act as any user; that is replaced here.

`team_id` still arrives in the body (a user belongs to many teams, so it is a
routing choice, not an identity claim) but is never trusted on its own: every
authenticated endpoint re-checks membership through the caller's OWN RLS
context. If the caller is not an active member, the row is invisible and the
request is refused — the check cannot be bypassed by lying about team_id.
"""
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from shared.config import settings
from shared.db import user_session

_bearer = HTTPBearer(auto_error=False)


def current_user_id(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    """Verify the bearer token and return the Supabase user id (`sub` claim)."""
    if not settings.supabase_jwt_secret:
        # Fail closed. An unset secret must not silently degrade to "trust the
        # caller" — that is exactly the hole this module exists to close.
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "SUPABASE_JWT_SECRET is not configured; refusing to authenticate",
        )
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        claims = jwt.decode(
            creds.credentials,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            audience="authenticated",
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, f"invalid token: {exc}"
        ) from exc
    subject = claims.get("sub")
    if not subject:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token has no subject")
    return str(subject)


def require_membership(user_id: str, team_id: str) -> None:
    """Refuse the request unless `user_id` is an active member of `team_id`.

    Runs as the user (role `authenticated`), so the membership row is subject to
    the same RLS the browser sees. A non-member gets no row back.
    """
    with user_session(user_id) as conn:
        row = conn.execute(
            "select 1 from public.memberships"
            " where team_id=%s and user_id=%s and status='active'",
            (team_id, user_id),
        ).fetchone()
    if row is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "not an active member of this team"
        )


CurrentUserId = Annotated[str, Depends(current_user_id)]
