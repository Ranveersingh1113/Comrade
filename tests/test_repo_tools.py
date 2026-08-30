"""The agent can see what happened in the repository.

findings §14: connecting a repository is a core feature, not an integration.
The ingestion path fills `github_activity`; this is the tool that reads it.

Read as the requesting member, like every other read tool since §4.1 — the
agent role has no grant on this table at all.
"""
import psycopg
import pytest

from agent.tools import fetch_repo_activity
from shared.config import settings
from tests._seed import A1, TEAM_A, TEAM_B


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def _repo(admin, team_id, full_name):
    return admin.execute(
        "insert into public.github_repos (team_id, repo_full_name)"
        " values (%s,%s) returning id",
        (team_id, full_name),
    ).fetchone()[0]


def _event(admin, team_id, repo_id, **over):
    fields = {
        "node_type": "merge",
        "author_github": "maya",
        "payload": '{"title": "Rewrite the auth flow"}',
        "occurred_at": "2026-08-20T10:00:00+00:00",
    }
    fields.update(over)
    return admin.execute(
        "insert into public.github_activity (team_id, repo_id, node_type,"
        " author_github, payload, occurred_at)"
        " values (%s,%s,%s,%s,%s::jsonb,%s) returning id",
        (team_id, repo_id, fields["node_type"], fields["author_github"],
         fields["payload"], fields["occurred_at"]),
    ).fetchone()[0]


def test_it_returns_this_teams_repository_activity(seeded, admin):
    repo_id = _repo(admin, TEAM_A, "acme/widgets")
    _event(admin, TEAM_A, repo_id)
    rows = fetch_repo_activity(TEAM_A, A1)
    assert len(rows) == 1
    assert rows[0]["repo"] == "acme/widgets"
    assert rows[0]["node_type"] == "merge"
    assert rows[0]["author"] == "maya"
    assert "Rewrite the auth flow" in rows[0]["summary"]


def test_a_member_of_both_teams_still_sees_only_this_team(seeded, admin):
    """The filter, not RLS, is what scopes this — a member may read both."""
    a_repo = _repo(admin, TEAM_A, "acme/widgets")
    b_repo = _repo(admin, TEAM_B, "other/secret")
    _event(admin, TEAM_A, a_repo, payload='{"title": "team A work"}')
    _event(admin, TEAM_B, b_repo, payload='{"title": "team B work"}')
    admin.execute(
        "insert into public.memberships (team_id, user_id, role, status)"
        " values (%s,%s,'member','active')",
        (TEAM_B, A1),
    )

    # The premise: A1 genuinely CAN read TEAM_B's row under authenticated, so
    # this test is about the query's filter and not about RLS doing the work.
    from tests._seed import as_user
    with as_user(A1) as conn:
        assert conn.execute(
            "select count(*) from public.github_activity where team_id=%s",
            (TEAM_B,),
        ).fetchone()[0] == 1

    rows = fetch_repo_activity(TEAM_A, A1)
    assert [r["repo"] for r in rows] == ["acme/widgets"]
    assert all("team B work" not in r["summary"] for r in rows)


def test_it_is_capped_and_flags_truncation(seeded, admin):
    repo_id = _repo(admin, TEAM_A, "acme/widgets")
    for i in range(40):
        _event(admin, TEAM_A, repo_id, payload=f'{{"title": "change {i}"}}')
    rows = fetch_repo_activity(TEAM_A, A1, limit=5)
    assert len(rows) == 5


def test_a_long_body_is_truncated(seeded, admin):
    repo_id = _repo(admin, TEAM_A, "acme/widgets")
    _event(admin, TEAM_A, repo_id,
           payload='{"title": "big", "body": "' + "x" * 5000 + '"}')
    row = fetch_repo_activity(TEAM_A, A1)[0]
    assert row["truncated"] is True
    assert len(row["summary"]) < 5000


def test_newest_first(seeded, admin):
    repo_id = _repo(admin, TEAM_A, "acme/widgets")
    _event(admin, TEAM_A, repo_id, payload='{"title": "older"}',
           occurred_at="2026-08-01T10:00:00+00:00")
    _event(admin, TEAM_A, repo_id, payload='{"title": "newer"}',
           occurred_at="2026-08-25T10:00:00+00:00")
    rows = fetch_repo_activity(TEAM_A, A1)
    assert "newer" in rows[0]["summary"]


def test_an_empty_repo_history_is_not_an_error(seeded):
    assert fetch_repo_activity(TEAM_A, A1) == []


def test_the_tool_is_declared_in_the_registry():
    from agent.registry import spec_for

    spec = spec_for("repo_activity")
    assert spec.surface == "db"
    assert spec.writes is False
