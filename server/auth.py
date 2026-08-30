"""Request authentication for the HTTP surface.

Identity comes from a Supabase-issued JWT and nothing else. The previous
dev-only contract read `requester_id` from the request body, which let any
caller act as any user; that is replaced here.

`team_id` still arrives in the body (a user belongs to many teams, so it is a
routing choice, not an identity claim) but is never trusted on its own: every
authenticated endpoint re-checks membership through the caller's OWN RLS
context. If the caller is not an active member, the row is invisible and the
request is refused — the check cannot be bypassed by lying about team_id.

Two signing schemes are supported because Supabase migrated between them:
current projects issue ASYMMETRIC tokens (ES256/RS256) whose public keys are
published at /auth/v1/.well-known/jwks.json, while legacy projects sign
symmetrically with the shared SUPABASE_JWT_SECRET. The algorithm is chosen from
the token header and each path only ever accepts its own algorithms — a token
must never be able to talk the verifier into a weaker scheme.
"""
from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from shared.config import settings
from shared.db import user_session

_bearer = HTTPBearer(auto_error=False)

# Asymmetric algorithms Supabase may use for user tokens.
_ASYMMETRIC = ("ES256", "RS256")


@lru_cache(maxsize=1)
def _jwk_client() -> jwt.PyJWKClient:
    """JWKS client for the project's auth server (caches keys internally)."""
    url = settings.supabase_url.rstrip("/")
    return jwt.PyJWKClient(f"{url}/auth/v1/.well-known/jwks.json")


def _decode(token: str) -> dict:
    """Verify `token` against whichever scheme its header declares."""
    try:
        alg = jwt.get_unverified_header(token).get("alg")
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, f"invalid token: {exc}"
        ) from exc

    if alg in _ASYMMETRIC:
        if not settings.supabase_url:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "SUPABASE_URL is not configured; cannot fetch JWKS",
            )
        key = _jwk_client().get_signing_key_from_jwt(token).key
        # Pinned to the asymmetric list, never widened by the header.
        return jwt.decode(
            token, key, algorithms=list(_ASYMMETRIC), audience="authenticated"
        )

    if not settings.supabase_jwt_secret:
        # Fail closed. A missing secret must not silently degrade to "trust the
        # caller" — that is exactly the hole this module exists to close.
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "SUPABASE_JWT_SECRET is not configured; refusing to authenticate",
        )
    return jwt.decode(
        token,
        settings.supabase_jwt_secret,
        algorithms=["HS256"],
        audience="authenticated",
    )


def current_user_id(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    """Verify the bearer token and return the Supabase user id (`sub` claim)."""
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        claims = _decode(creds.credentials)
    except HTTPException:
        raise
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
