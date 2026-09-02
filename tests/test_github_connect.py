"""Connecting a team's GitHub account, and proving they own it.

GitHub redirects back with `installation_id` as an unauthenticated number in a
URL, and installation ids are small sequential integers. So "record whatever
comes back" means anyone can claim somebody else's installation by guessing —
and the unique constraint then makes it FIRST-CLAIM-WINS: the attacker gets
the row and the rightful owner gets a constraint violation.

These are the three checks that stop that, and each is tested by breaking it.
"""
import time

import psycopg
import pytest

from server.github_connect import (
    ConnectError, _read_state, _state_for, install_url, record_installation,
)
from shared.config import settings
from tests._seed import A1, A2, B1, TEAM_A, TEAM_B

INSTALL_A = 313131


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.execute("delete from public.github_repos where team_id in (%s,%s)",
                     (TEAM_A, TEAM_B))
        conn.execute(
            "delete from public.github_installations where team_id in (%s,%s)",
            (TEAM_A, TEAM_B))
        conn.close()


@pytest.fixture
def github(monkeypatch):
    """GitHub says yes to everything, so a refusal in a test below is OUR
    refusal and not a mocked-out network error wearing its coat."""
    monkeypatch.setattr(
        "server.github_connect._user_token", lambda code: "gho_user")
    monkeypatch.setattr(
        "server.github_connect._user_administers", lambda tok, iid: True)
    monkeypatch.setattr(
        "server.github_connect._account_login", lambda iid: "acme-org")


# ---------------------------------------------------------------------------
# The state token: the install started with us, for this team
# ---------------------------------------------------------------------------

def test_the_team_comes_from_the_signed_state_not_the_url(seeded, admin, github):
    """🔴 The path parameter is attacker-controlled; the state token is not.

    A request naming team B in the URL while carrying team A's state must land
    in A. Otherwise the route is a way to write an installation into any team
    whose id you can type.
    """
    state = _state_for(TEAM_A, A1)
    out = record_installation(
        installation_id=INSTALL_A, state=state, code="c", session_user_id=A1
    )
    assert out["team_id"] == TEAM_A

    row = admin.execute(
        "select team_id from public.github_installations where installation_id=%s",
        (INSTALL_A,),
    ).fetchone()
    assert str(row[0]) == TEAM_A


def test_a_forged_state_is_refused(seeded, admin, github):
    with pytest.raises(ConnectError, match="not valid any more"):
        record_installation(
            installation_id=INSTALL_A, state="not.a.token",
            code="c", session_user_id=A1,
        )


def test_an_expired_state_is_refused(seeded, admin, github, monkeypatch):
    """A state token copied out of a browser's history must go stale."""
    monkeypatch.setattr("server.github_connect._STATE_TTL_SECONDS", -1)
    stale = _state_for(TEAM_A, A1)
    with pytest.raises(ConnectError, match="not valid any more"):
        record_installation(
            installation_id=INSTALL_A, state=stale, code="c", session_user_id=A1
        )


def test_a_different_account_cannot_finish_someone_elses_install(
    seeded, admin, github
):
    """🔴 State alone is not enough.

    A state token leaked from a browser's history would otherwise let a
    different logged-in account complete the connection — into the team the
    token names, which is not their team.
    """
    state = _state_for(TEAM_A, A1)
    with pytest.raises(ConnectError, match="different account"):
        record_installation(
            installation_id=INSTALL_A, state=state, code="c", session_user_id=A2
        )


# ---------------------------------------------------------------------------
# The ownership check: GitHub's answer, not ours
# ---------------------------------------------------------------------------

def test_an_installation_the_user_cannot_administer_is_refused(
    seeded, admin, monkeypatch
):
    """🔴 The check that makes the guessable id harmless.

    installation_id arrives as a number in a URL. Only GitHub can say whether
    the person holding this session can administer it, so only GitHub is
    asked.
    """
    monkeypatch.setattr("server.github_connect._user_token", lambda c: "gho_user")
    monkeypatch.setattr(
        "server.github_connect._user_administers", lambda tok, iid: False)

    with pytest.raises(ConnectError, match="cannot? administer|administer"):
        record_installation(
            installation_id=INSTALL_A, state=_state_for(TEAM_A, A1),
            code="c", session_user_id=A1,
        )
    assert admin.execute(
        "select count(*) from public.github_installations where installation_id=%s",
        (INSTALL_A,),
    ).fetchone()[0] == 0


def test_a_deployment_with_no_oauth_credentials_refuses_rather_than_skips(
    seeded, admin, monkeypatch
):
    """🔴 The dangerous default.

    With no client id and secret there is no way to verify ownership. Treating
    that as "verification unavailable, proceed" would silently turn the check
    off in exactly the deployments least likely to notice.
    """
    monkeypatch.setattr("shared.config.settings.github_app_client_id", "")
    monkeypatch.setattr("shared.config.settings.github_app_client_secret", "")
    with pytest.raises(ConnectError, match="cannot verify who owns"):
        record_installation(
            installation_id=INSTALL_A, state=_state_for(TEAM_A, A1),
            code="c", session_user_id=A1,
        )


# ---------------------------------------------------------------------------
# RLS still decides tenancy
# ---------------------------------------------------------------------------

def test_a_plain_member_cannot_connect_an_installation(seeded, admin, github):
    """The server vouches for the GitHub half only. Whether this person may
    connect anything to this team at all is RLS's answer, and A2 is a member
    rather than a leader."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        record_installation(
            installation_id=INSTALL_A, state=_state_for(TEAM_A, A2),
            code="c", session_user_id=A2,
        )


def test_an_installation_already_owned_by_another_team_is_not_stolen(
    seeded, admin, github
):
    """The upsert updates only when the row already belongs to this team, so a
    second team claiming a live installation changes nothing."""
    admin.execute(
        "insert into public.github_installations"
        " (team_id, installation_id, account_login) values (%s,%s,%s)",
        (TEAM_B, INSTALL_A, "b-org"),
    )
    record_installation(
        installation_id=INSTALL_A, state=_state_for(TEAM_A, A1),
        code="c", session_user_id=A1,
    )
    row = admin.execute(
        "select team_id, account_login from public.github_installations"
        " where installation_id=%s", (INSTALL_A,),
    ).fetchone()
    assert str(row[0]) == TEAM_B
    assert row[1] == "b-org"


# ---------------------------------------------------------------------------
# The install link
# ---------------------------------------------------------------------------

def test_no_app_configured_says_so_instead_of_offering_a_broken_link(
    seeded, monkeypatch
):
    monkeypatch.setattr("shared.config.settings.github_app_id", "")
    monkeypatch.setattr("shared.config.settings.github_app_private_key", "")
    out = install_url(TEAM_A, A1)
    assert out["configured"] is False
    assert "reason" in out


def test_the_install_link_carries_a_state_that_round_trips(seeded, monkeypatch):
    monkeypatch.setattr("shared.config.settings.github_app_id", "12345")
    monkeypatch.setattr("shared.config.settings.github_app_private_key", "x")
    monkeypatch.setattr("shared.config.settings.github_app_slug", "comrade")
    out = install_url(TEAM_A, A1)
    assert out["configured"] is True
    state = out["url"].split("state=")[1]
    assert _read_state(state) == (TEAM_A, A1)


# ---------------------------------------------------------------------------
# Deliveries: routed by installation, and an uninstall is acted on
# ---------------------------------------------------------------------------

def test_a_delivery_routes_by_installation_not_by_repository_name(seeded, admin):
    """🔴 Two teams may legitimately connect the same public repository.

    resolve_team_for_repo returns whichever row came back first, which would
    route one team's activity into another team's wiki. An installation id is
    unique across all teams, so it cannot be ambiguous — and the payload
    always carries one.
    """
    from pipeline.github import resolve_team_for_installation

    admin.execute(
        "insert into public.github_installations"
        " (team_id, installation_id, account_login) values (%s,%s,%s)",
        (TEAM_B, INSTALL_A, "b-org"),
    )
    admin.execute(
        "insert into public.github_repos (team_id, repo_full_name) values (%s,%s)",
        (TEAM_A, "shared/oss"),
    )
    admin.execute(
        "insert into public.github_repos"
        " (team_id, repo_full_name, installation_id) values (%s,%s,%s)",
        (TEAM_B, "shared/oss", INSTALL_A),
    )
    # By name this is ambiguous; by installation it is not.
    assert resolve_team_for_installation(INSTALL_A) == TEAM_B


def test_an_uninstall_disconnects_the_repositories_with_it(seeded, admin):
    """🔴 Nothing would otherwise notice.

    An uninstalled App leaves rows whose every sync fails on a revoked
    credential and burns three retries each time — and leaves the team's UI
    claiming a repository is connected that nothing can read.
    """
    from pipeline.github import forget_installation

    admin.execute(
        "insert into public.github_installations"
        " (team_id, installation_id, account_login) values (%s,%s,%s)",
        (TEAM_A, INSTALL_A, "acme"),
    )
    admin.execute(
        "insert into public.github_repos"
        " (team_id, repo_full_name, installation_id) values (%s,%s,%s)",
        (TEAM_A, "acme/app", INSTALL_A),
    )
    forget_installation(INSTALL_A)

    assert admin.execute(
        "select count(*) from public.github_installations where installation_id=%s",
        (INSTALL_A,),
    ).fetchone()[0] == 0
    # The repository went with it, via the foreign key rather than a second
    # delete anyone has to remember to write.
    assert admin.execute(
        "select count(*) from public.github_repos where team_id=%s", (TEAM_A,)
    ).fetchone()[0] == 0
