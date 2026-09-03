"""Connecting a team's GitHub account, and proving they own it.

Three things have to be true before an installation row is written, and only
the first is obvious:

  1. The person is a member of the team. `require_membership` in the route.
  2. The install was STARTED by us, for this team. Otherwise a link crafted by
     anyone lands a stranger's installation in whichever team the URL names.
     That is the `state` token below.
  3. The person can actually ADMINISTER the installation they are claiming.
     This is the one that is easy to skip and expensive to skip.

WHY (3) NEEDS THE OAUTH ROUND TRIP
------------------------------------
GitHub redirects back with `installation_id` as a query parameter, in the
user's own browser. Nothing about that parameter is authenticated — it is a
number in a URL. Installation ids are small sequential integers, so "record
whatever comes back" means anyone can claim somebody else's installation by
guessing, and the unique constraint then makes it FIRST-CLAIM-WINS: the
attacker gets the row and the rightful owner gets a constraint violation.

`state` alone does not fix that. It proves the flow started with us for this
team, which stops a crafted link — but the person holding a valid state is
free to substitute a different installation_id on the way back.

So the App requests user authorization during installation, GitHub also sends
a `code`, and that exchanges for a token that acts AS THE USER. Asking GitHub
"which installations can this user see" is the only answer that comes from the
party that actually knows.
"""
import logging
import time

import httpx
import jwt

from shared.config import settings
from shared.db import Role, connect, user_session
from shared.github_app import GitHubAppError, configured, repositories

logger = logging.getLogger(__name__)

GITHUB = "https://github.com"
GITHUB_API = "https://api.github.com"

#: Long enough to install an App and pick repositories, short enough that a
#: state token copied out of a browser's history is useless by the time anyone
#: finds it.
_STATE_TTL_SECONDS = 900


class ConnectError(Exception):
    """The installation could not be connected."""


def _state_for(team_id: str, user_id: str) -> str:
    """A signed, short-lived token binding this install attempt to this team
    and this person.

    Signed rather than stored: there is nothing to clean up, nothing to leak,
    and no table to consult on the way back. The Supabase JWT secret is
    already the thing this deployment trusts to say who someone is.
    """
    now = int(time.time())
    return jwt.encode(
        {"team_id": team_id, "user_id": user_id,
         "iat": now, "exp": now + _STATE_TTL_SECONDS, "aud": "github-install"},
        settings.supabase_jwt_secret,
        algorithm="HS256",
    )


def _read_state(state: str) -> tuple[str, str]:
    try:
        claims = jwt.decode(
            state, settings.supabase_jwt_secret,
            algorithms=["HS256"], audience="github-install",
        )
    except jwt.PyJWTError as exc:
        # No detail. A precise error here tells whoever is probing which half
        # of the token they got wrong.
        raise ConnectError("this install link is not valid any more. Start"
                           " again from the setup screen.") from exc
    return claims["team_id"], claims["user_id"]


def install_url(team_id: str, user_id: str) -> dict:
    """Where to send someone to install the App, or why we cannot."""
    if not configured():
        return {
            "configured": False,
            "reason": "No GitHub App is set up for this deployment.",
        }
    if not settings.github_app_slug:
        return {"configured": False, "reason": "GITHUB_APP_SLUG is not set."}
    return {
        "configured": True,
        "url": (
            f"{GITHUB}/apps/{settings.github_app_slug}/installations/new"
            f"?state={_state_for(team_id, user_id)}"
        ),
    }


def _user_token(code: str) -> str:
    """Exchange the redirect's `code` for a token that acts as the USER.

    Not an installation token: this one answers "what can this person see",
    which is the question we actually have.
    """
    if not (settings.github_app_client_id and settings.github_app_client_secret):
        raise ConnectError(
            "this deployment cannot verify who owns an installation:"
            " GITHUB_APP_CLIENT_ID and GITHUB_APP_CLIENT_SECRET are not set."
            " Enable 'Request user authorization (OAuth) during installation'"
            " on the App and set both."
        )
    resp = httpx.post(
        f"{GITHUB}/login/oauth/access_token",
        headers={"Accept": "application/json"},
        data={
            "client_id": settings.github_app_client_id,
            "client_secret": settings.github_app_client_secret,
            "code": code,
        },
        timeout=30.0,
    )
    body = resp.json() if resp.status_code == 200 else {}
    token = body.get("access_token")
    if not token:
        raise ConnectError(
            "GitHub would not confirm who you are. Start the install again."
        )
    return token


def _user_administers(user_token: str, installation_id: int) -> bool:
    """Whether this person can see this installation.

    GitHub's answer, not ours. `/user/installations` returns exactly the
    installations the token's owner has access to, so a claim for anyone
    else's simply is not in the list.
    """
    page = 1
    while page <= 10:
        resp = httpx.get(
            f"{GITHUB_API}/user/installations",
            headers={
                "Authorization": f"Bearer {user_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            params={"per_page": 100, "page": page},
            timeout=30.0,
        )
        if resp.status_code != 200:
            raise ConnectError(
                f"GitHub would not list your installations ({resp.status_code})."
            )
        items = resp.json().get("installations", [])
        if any(int(i["id"]) == installation_id for i in items):
            return True
        if len(items) < 100:
            return False
        page += 1
    return False


def record_installation(
    *, installation_id: int, state: str, code: str, session_user_id: str
) -> dict:
    """Write the installation row, having established all three claims.

    The row is written AS THE USER, so RLS decides whether they may connect
    anything to this team at all — this function vouches only for the GitHub
    half. That split is deliberate: it keeps tenancy in the policies, where
    the frontend's direct Supabase access is also subject to it.
    """
    team_id, state_user_id = _read_state(state)

    # The person finishing the install must be the one who started it. Without
    # this, a state token leaked from someone's history lets a different
    # logged-in account complete the connection into that team.
    if state_user_id != session_user_id:
        raise ConnectError(
            "this install was started by a different account. Start again from"
            " your own setup screen."
        )

    if not _user_administers(_user_token(code), installation_id):
        raise ConnectError(
            "that installation does not belong to an account you can"
            " administer."
        )

    try:
        account = _account_login(installation_id)
    except GitHubAppError as exc:
        raise ConnectError(str(exc)) from exc

    with user_session(session_user_id) as conn:
        conn.execute(
            "insert into public.github_installations"
            " (team_id, installation_id, account_login) values (%s,%s,%s)"
            " on conflict (installation_id) do update"
            "   set account_login = excluded.account_login"
            " where public.github_installations.team_id = excluded.team_id",
            (team_id, installation_id, account),
        )
    return {"team_id": team_id, "installation_id": installation_id,
            "account_login": account}


def _account_login(installation_id: int) -> str:
    """Which org or user the App was installed on, from GitHub."""
    from shared.github_app import _app_jwt  # local: keeps the key in one module

    resp = httpx.get(
        f"{GITHUB_API}/app/installations/{installation_id}",
        headers={
            "Authorization": f"Bearer {_app_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise GitHubAppError(
            f"GitHub would not describe installation {installation_id}"
            f" ({resp.status_code})."
        )
    return str((resp.json().get("account") or {}).get("login", "unknown"))


def connectable_repositories(team_id: str, user_id: str) -> dict:
    """Every repository this team's installations can reach.

    This is what the picker offers, and offering exactly this is the point: a
    member chooses from what they have already granted rather than typing a
    name we would then have to decide whether to trust.
    """
    with user_session(user_id) as conn:
        installs = conn.execute(
            "select installation_id, account_login"
            " from public.github_installations where team_id = %s"
            " order by account_login",
            (team_id,),
        ).fetchall()
        connected = {
            name: cloned for name, cloned in conn.execute(
                "select repo_full_name, last_cloned_at from public.github_repos"
                " where team_id = %s", (team_id,)
            ).fetchall()
        }
    failures = _sync_failures(team_id)

    out = []
    for installation_id, account in installs:
        try:
            names = repositories(int(installation_id))
        except GitHubAppError as exc:
            # One broken installation must not hide the others. A team that
            # revoked one grant still needs to see and manage the rest.
            logger.warning("listing installation %s failed: %s",
                           installation_id, exc)
            out.append({"installation_id": int(installation_id),
                        "account_login": account, "error": str(exc),
                        "repositories": []})
            continue
        out.append({
            "installation_id": int(installation_id),
            "account_login": account,
            "repositories": [
                {
                    "full_name": n,
                    "connected": n in connected,
                    # Three states, not two. "Connected" alone was a lie
                    # whenever the clone had failed: the setup screen said
                    # CONNECTED while the agent said no repository was
                    # connected, and nothing anywhere named the credential
                    # error that caused it.
                    "cloned_at": connected[n].isoformat() if connected.get(n) else None,
                    "sync_error": failures.get(n),
                }
                for n in names
            ],
        })
    return {"installations": out}


def _sync_failures(team_id: str) -> dict[str, str]:
    """The last error for each repository whose sync failed.

    Read on the control-plane connection because members have no grant on
    `jobs` at all — the queue is infrastructure and not team data. Scoped to
    this team explicitly, and reached only after require_membership, which is
    the same shape resolve_team_for_repo uses for the same reason.

    Only failures matter here: a job that succeeded is described better by
    last_cloned_at, which is on the row the member can already see.
    """
    with connect(Role.ADMIN) as conn:
        rows = conn.execute(
            "select distinct on (payload->>'repo_full_name')"
            "       payload->>'repo_full_name', last_error"
            "  from public.jobs"
            " where team_id = %s and job_type = 'sync_repo' and status = 'failed'"
            " order by payload->>'repo_full_name', finished_at desc",
            (team_id,),
        ).fetchall()
    return {name: (err or "the clone failed")[:300] for name, err in rows if name}
