"""A GitHub credential that belongs to one team.

The function these tests are about, pipeline/repo_sync._token_for, used to
ignore both of its arguments and return one process-wide PAT — so a team's
checkout was fetched with a credential scoped to every repository its owner
could reach. Every other isolation property in this system was undone by it.

So the tests here are almost entirely about REFUSAL: what a team cannot reach,
and what happens when a credential stops being safe to use.
"""
import psycopg
import pytest

from pipeline.repo_sync import _pat_is_still_single_tenant, _token_for
from pipeline.worker import PermanentJobError
from shared.config import settings
from shared.db import user_session
from tests._seed import A1, B1, TEAM_A, TEAM_B

REPO_A = "team-a/app"
REPO_B = "team-b/secrets"
INSTALL_A = 111111
INSTALL_B = 222222


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


def _install(admin, team_id, installation_id, login):
    admin.execute(
        "insert into public.github_installations"
        " (team_id, installation_id, account_login) values (%s,%s,%s)",
        (team_id, installation_id, login),
    )


def _connect_repo(admin, team_id, full_name, installation_id):
    admin.execute(
        "insert into public.github_repos"
        " (team_id, repo_full_name, installation_id) values (%s,%s,%s)",
        (team_id, full_name, installation_id),
    )


# ---------------------------------------------------------------------------
# The hole this closed
# ---------------------------------------------------------------------------

def test_the_local_pat_is_refused_once_a_second_team_connects_a_repo(
    seeded, admin, monkeypatch
):
    """🔴 The reason any of this exists.

    The PAT is scoped to everything its owner can reach. With one developer
    and one repository that is "my own credential for my own code". The moment
    a second team connects a repository it becomes "this team's agent holds
    that team's access", and nothing about the code changed to make it so —
    only the data did.

    Refused rather than warned about: a warning in a log is not a boundary.
    """
    monkeypatch.setattr("shared.config.settings.github_pat", "ghp_local")
    _connect_repo_no_install(admin, TEAM_A, REPO_A)
    assert _token_for(TEAM_A, REPO_A) == "ghp_local"

    _connect_repo_no_install(admin, TEAM_B, REPO_B)
    assert _pat_is_still_single_tenant() is False
    with pytest.raises(PermanentJobError, match="more than one team"):
        _token_for(TEAM_A, REPO_A)


def _connect_repo_no_install(admin, team_id, full_name):
    admin.execute(
        "insert into public.github_repos (team_id, repo_full_name)"
        " values (%s,%s)",
        (team_id, full_name),
    )


def test_a_team_cannot_connect_a_repo_through_another_teams_installation(
    seeded, admin
):
    """🔴 The attack the installation column would otherwise invite.

    Team B installs the App and grants it their private repositories. Team A's
    leader inserts a github_repos row naming B's installation id. If that were
    allowed, A's next sync would mint a token scoped to B's repositories and
    clone one.

    RLS refuses it, which matters more than a server-side check would: the
    frontend talks to Supabase directly, so the policy IS the API. A check
    that lived only in an endpoint would be bypassed by anyone posting to
    PostgREST themselves.
    """
    _install(admin, TEAM_B, INSTALL_B, "team-b")

    # As team A's LEADER — the role that is allowed to connect a repository at
    # all. A test that used a plain member would pass for the wrong reason.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with user_session(A1) as conn:
            conn.execute(
                "insert into public.github_repos"
                " (team_id, repo_full_name, installation_id) values (%s,%s,%s)",
                (TEAM_A, "team-b/secrets", INSTALL_B),
            )

    # And the same leader CAN connect through their own installation, so the
    # refusal above is about whose installation it is and not about the
    # insert being blocked outright.
    _install(admin, TEAM_A, INSTALL_A, "team-a")
    with user_session(A1) as conn:
        conn.execute(
            "insert into public.github_repos"
            " (team_id, repo_full_name, installation_id) values (%s,%s,%s)",
            (TEAM_A, REPO_A, INSTALL_A),
        )


def test_a_repo_connected_with_no_installation_cannot_be_fetched_at_all(
    seeded, admin, monkeypatch
):
    """With no App and no PAT there is no credential, and saying so beats
    failing later inside git with an authentication error that reads like a
    revoked grant."""
    monkeypatch.setattr("shared.config.settings.github_pat", "")
    _connect_repo_no_install(admin, TEAM_A, REPO_A)
    with pytest.raises(PermanentJobError, match="no GitHub credential reaches"):
        _token_for(TEAM_A, REPO_A)


# ---------------------------------------------------------------------------
# The installation path
# ---------------------------------------------------------------------------

def test_the_installation_for_a_repo_is_this_teams_installation(
    seeded, admin, monkeypatch
):
    """Both teams connect the SAME public repository, which is legitimate and
    is exactly where a lookup by name alone goes wrong: it would find whichever
    row came back first and mint that team's token."""
    _install(admin, TEAM_A, INSTALL_A, "team-a")
    _install(admin, TEAM_B, INSTALL_B, "team-b")
    _connect_repo(admin, TEAM_A, "shared/oss", INSTALL_A)
    _connect_repo(admin, TEAM_B, "shared/oss", INSTALL_B)

    minted: list[int] = []
    monkeypatch.setattr(
        "pipeline.repo_sync.installation_token",
        lambda iid: minted.append(iid) or f"ghs_for_{iid}",
    )
    assert _token_for(TEAM_A, "shared/oss") == f"ghs_for_{INSTALL_A}"
    assert _token_for(TEAM_B, "shared/oss") == f"ghs_for_{INSTALL_B}"
    assert minted == [INSTALL_A, INSTALL_B]


def test_the_installation_beats_a_configured_pat(seeded, admin, monkeypatch):
    """A leftover GITHUB_PAT in an environment must not quietly take priority
    over the scoped credential — that would reintroduce the broad token on a
    system that had already been migrated off it."""
    monkeypatch.setattr("shared.config.settings.github_pat", "ghp_local")
    _install(admin, TEAM_A, INSTALL_A, "team-a")
    _connect_repo(admin, TEAM_A, REPO_A, INSTALL_A)
    monkeypatch.setattr(
        "pipeline.repo_sync.installation_token", lambda iid: f"ghs_for_{iid}"
    )
    assert _token_for(TEAM_A, REPO_A) == f"ghs_for_{INSTALL_A}"


def test_a_revoked_installation_is_permanent_not_retried(
    seeded, admin, monkeypatch
):
    """Nothing about retrying makes a removed grant come back, and burning
    three attempts to discover that delays the message the team must act on."""
    from shared.github_app import GitHubAppError

    _install(admin, TEAM_A, INSTALL_A, "team-a")
    _connect_repo(admin, TEAM_A, REPO_A, INSTALL_A)

    def refuse(_iid):
        raise GitHubAppError("GitHub refused a token for installation (404)")

    monkeypatch.setattr("pipeline.repo_sync.installation_token", refuse)
    with pytest.raises(PermanentJobError, match="refused a token"):
        _token_for(TEAM_A, REPO_A)


def test_one_installation_belongs_to_exactly_one_team(seeded, admin):
    """🔴 The uniqueness that makes webhook routing unambiguous.

    github_repos' (team_id, repo_full_name) never had this property, so two
    teams could register the same repository and resolve_team_for_repo picked
    whichever row came back first. An installation id is global, so claiming
    one someone else holds is a constraint violation rather than a silent
    second owner.
    """
    _install(admin, TEAM_A, INSTALL_A, "team-a")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _install(admin, TEAM_B, INSTALL_A, "team-a")


def test_a_team_on_the_app_does_not_block_another_teams_local_pat(
    seeded, admin, monkeypatch
):
    """🔴 A refusal with nobody on the other side of it.

    The guard counted teams with a repository. But a team whose repositories
    all have an installation never reaches the PAT at all — so a team
    completing its migration to the GitHub App would switch off an unrelated
    developer's local credential, protecting them from a sharing that was not
    happening.

    Found the way these things are: a real connected repository in a
    developer's own database started failing a test about a credential it does
    not use.
    """
    monkeypatch.setattr("shared.config.settings.github_pat", "ghp_local")
    _connect_repo_no_install(admin, TEAM_A, REPO_A)
    assert _token_for(TEAM_A, REPO_A) == "ghp_local"

    # Team B is fully on the App. That must change nothing for team A.
    _install(admin, TEAM_B, INSTALL_B, "team-b")
    _connect_repo(admin, TEAM_B, REPO_B, INSTALL_B)
    assert _pat_is_still_single_tenant() is True
    assert _token_for(TEAM_A, REPO_A) == "ghp_local"
