"""Minting a GitHub credential that belongs to one team.

This replaces a process-wide PAT whose scope was "everything its owner can
reach". An installation token is scoped by GitHub to the repositories that
installation was granted, and expires in an hour, so the authorization is
enforced on every request by the party that owns the data rather than by us
remembering to check.

NOTHING HERE IS EVER WRITTEN DOWN
-----------------------------------
No token column, no token file, no token in a log line. Tokens live in this
process's memory for their hour and are re-minted after. A token at rest is a
token in every backup, every replica, and every `select *` a support script
ever runs — and unlike a password, nobody ever rotates one they forgot exists.

The App's private key is the one long-lived secret, and it is the only thing
that can mint anything. It is read from the environment and never leaves this
module.

TWO HOPS, WHICH IS EASY TO GET SUBTLY WRONG
---------------------------------------------
  1. A JWT signed with the App's private key. Proves "I am this App". Good for
     ten minutes at most, by GitHub's rule.
  2. POST that JWT to /app/installations/{id}/access_tokens. Returns the token
     that actually reads repositories.

Step 1's token cannot read a repository and step 2's cannot mint another. Using
the wrong one gives a 401 that reads like a credential problem rather than a
protocol one, which is why they have different names here.
"""
import base64
import logging
import threading
import time
from dataclasses import dataclass

import httpx
import jwt

from shared.config import settings

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

#: GitHub refuses an App JWT whose lifetime exceeds ten minutes. Nine leaves
#: room for the clock skew the `iat` backdate below also guards against.
_JWT_LIFETIME_SECONDS = 540

#: An installation token is valid for an hour. Re-minting at fifty minutes
#: means a long turn never hands a token to git that expires mid-clone —
#: "Authentication failed" halfway through a fetch is indistinguishable from a
#: revoked installation, and would send anyone debugging it to the wrong place.
_REFRESH_MARGIN_SECONDS = 600


class GitHubAppError(Exception):
    """The App could not mint a credential."""


@dataclass(frozen=True)
class _Cached:
    token: str
    expires_at: float


_cache: dict[int, _Cached] = {}
#: Two turns for the same team can mint at once. Without this they race to the
#: same API and both write the cache; harmless today, but the lock also makes
#: "one token per installation in flight" true, which is what keeps us off
#: GitHub's rate limit when a team has several members talking at once.
_lock = threading.Lock()


def configured() -> bool:
    """Whether an App is set up at all. The connect UI asks this before
    offering an install link it cannot complete."""
    return bool(settings.github_app_id and settings.github_app_private_key)


def _private_key() -> str:
    """The PEM, however the environment happened to carry it.

    A PEM is multi-line and environments disagree about that: .env files
    usually cannot hold real newlines, docker-compose and CI secrets often can,
    and base64 is what people reach for when neither works. Accepting all three
    costs six lines and removes a deployment failure whose symptom is an
    unhelpful "could not deserialize key data".
    """
    raw = settings.github_app_private_key.strip()
    if not raw:
        raise GitHubAppError(
            "no GitHub App private key is configured, so no repository can be"
            " reached. Set GITHUB_APP_PRIVATE_KEY."
        )
    if "BEGIN" not in raw:
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitHubAppError(
                "GITHUB_APP_PRIVATE_KEY is neither a PEM nor valid base64."
            ) from exc
    return raw.replace("\\n", "\n")


def _app_jwt() -> str:
    """Proves we are this App. Not a credential for any repository."""
    now = int(time.time())
    try:
        return jwt.encode(
            {
                # Backdated a minute: GitHub rejects a JWT issued in ITS
                # future, and a host clock a few seconds fast is common enough
                # that the standard advice is to not rely on being right.
                "iat": now - 60,
                "exp": now + _JWT_LIFETIME_SECONDS,
                "iss": settings.github_app_id,
            },
            _private_key(),
            algorithm="RS256",
        )
    except Exception as exc:  # pyjwt raises several unrelated types here
        raise GitHubAppError(f"could not sign the App JWT: {type(exc).__name__}") from exc


def installation_token(installation_id: int) -> str:
    """A token scoped to exactly the repositories this installation was granted.

    Cached until shortly before it expires. Never logged, never persisted.
    """
    now = time.time()
    with _lock:
        hit = _cache.get(installation_id)
        if hit and hit.expires_at - _REFRESH_MARGIN_SECONDS > now:
            return hit.token

        resp = httpx.post(
            f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {_app_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )
        if resp.status_code != 201:
            # GitHub's own message, when there is one. Never the raw body: it
            # echoes request detail, and this string ends up in job rows and
            # log lines. `.json()` on an error page raises, so it is guarded
            # rather than assumed — a parse failure while reporting a failure
            # replaces a useful message with a useless one.
            try:
                detail = str(resp.json().get("message", ""))[:200]
            except ValueError:
                detail = ""
            raise GitHubAppError(
                f"GitHub refused a token for installation {installation_id}"
                f" ({resp.status_code}){': ' + detail if detail else ''}"
            )

        data = resp.json()
        token = data["token"]
        # GitHub states the expiry; trusting it beats assuming an hour, since
        # a token minted against a suspended installation comes back short.
        expires_at = _parse_expiry(data.get("expires_at"), now)
        _cache[installation_id] = _Cached(token=token, expires_at=expires_at)
        return token


def _parse_expiry(value: str | None, now: float) -> float:
    """GitHub's ISO-8601 expiry, or an hour from now if it is missing."""
    if not value:
        return now + 3600
    try:
        from datetime import datetime

        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return now + 3600


def forget(installation_id: int) -> None:
    """Drop a cached token. Called when an installation is disconnected, so a
    token minted seconds earlier cannot outlive the connection that justified
    it."""
    with _lock:
        _cache.pop(installation_id, None)


def repositories(installation_id: int) -> list[str]:
    """Every repository this installation can reach, as `owner/name`.

    This is what the connect UI offers, and offering exactly this is the
    point: a member picks from what they have already granted, rather than
    typing a name we then have to decide whether to trust.
    """
    token = installation_token(installation_id)
    names: list[str] = []
    page = 1
    while True:
        resp = httpx.get(
            f"{GITHUB_API}/installation/repositories",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            params={"per_page": 100, "page": page},
            timeout=30.0,
        )
        if resp.status_code != 200:
            raise GitHubAppError(
                f"could not list repositories for installation"
                f" {installation_id} ({resp.status_code})"
            )
        body = resp.json()
        batch = body.get("repositories", [])
        names.extend(r["full_name"] for r in batch)
        # A page shorter than the limit is the last one. Bounded at 10 pages so
        # an installation on a very large org cannot spin here forever.
        if len(batch) < 100 or page >= 10:
            return names
        page += 1
