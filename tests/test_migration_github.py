"""Schema assertions for repository ingestion.

findings §14: the schema always assumed this feature — `github_repos` and
`github_activity` landed in migration one, `memory_citations.source_kind`
already accepts 'github', `contribution_v` already counts `github_events`.
What was missing is that nothing could write those tables.
"""
import psycopg
import pytest

from shared.config import settings
from shared.db import Role, team_session
from tests._seed import A1, TEAM_A, as_user


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def _repo(admin, full_name="acme/widgets"):
    return admin.execute(
        "insert into public.github_repos (team_id, repo_full_name)"
        " values (%s,%s) returning id",
        (TEAM_A, full_name),
    ).fetchone()[0]


@pytest.mark.parametrize(
    "node_type", ["commit", "pr", "merge", "issue", "review", "comment", "branch"]
)
def test_every_delivered_node_type_is_accepted(seeded, admin, node_type):
    repo_id = _repo(admin, f"acme/{node_type}")
    admin.execute(
        "insert into public.github_activity (team_id, repo_id, node_type)"
        " values (%s,%s,%s)",
        (TEAM_A, repo_id, node_type),
    )


def test_an_unknown_node_type_is_still_rejected(seeded, admin):
    """Widening the check must not have removed it."""
    repo_id = _repo(admin)
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute(
            "insert into public.github_activity (team_id, repo_id, node_type)"
            " values (%s,%s,'banana')",
            (TEAM_A, repo_id),
        )


def test_the_queue_accepts_an_ingest_github_job(seeded, admin):
    admin.execute(
        "insert into public.jobs (team_id, job_type) values (%s,'ingest_github')",
        (TEAM_A,),
    )


def test_the_pipeline_can_write_activity_and_read_repos(seeded, admin):
    repo_id = _repo(admin)
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        assert conn.execute(
            "select count(*) from public.github_repos where team_id=%s", (TEAM_A,)
        ).fetchone()[0] == 1
        conn.execute(
            "insert into public.github_activity (team_id, repo_id, node_type,"
            " author_github) values (%s,%s,'pr','maya')",
            (TEAM_A, repo_id),
        )


def test_a_member_may_read_repository_history(seeded, admin):
    repo_id = _repo(admin)
    admin.execute(
        "insert into public.github_activity (team_id, repo_id, node_type)"
        " values (%s,%s,'commit')",
        (TEAM_A, repo_id),
    )
    with as_user(A1) as conn:
        assert conn.execute(
            "select count(*) from public.github_activity where team_id=%s",
            (TEAM_A,),
        ).fetchone()[0] == 1


def test_a_member_may_not_author_repository_history(seeded, admin):
    """A member inserting here would be fabricating repository events that the
    compiler then turns into cited wiki facts — putting words in the repo's
    mouth. Supabase's default grant allowed exactly that."""
    repo_id = _repo(admin)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with as_user(A1) as conn:
            conn.execute(
                "insert into public.github_activity (team_id, repo_id, node_type)"
                " values (%s,%s,'commit')",
                (TEAM_A, repo_id),
            )


def test_a_member_may_not_rewrite_repository_history(seeded, admin):
    repo_id = _repo(admin)
    admin.execute(
        "insert into public.github_activity (team_id, repo_id, node_type,"
        " author_github) values (%s,%s,'commit','maya')",
        (TEAM_A, repo_id),
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with as_user(A1) as conn:
            conn.execute(
                "update public.github_activity set author_github='someone-else'"
            )


def test_a_github_citation_is_accepted_by_the_source_team_trigger(seeded, admin):
    """trg_memory_citation_source_team's else-branch already looks up
    github_activity, so citations need no schema work — but nothing had ever
    exercised it, because nothing wrote the table."""
    repo_id = _repo(admin)
    activity_id = admin.execute(
        "insert into public.github_activity (team_id, repo_id, node_type)"
        " values (%s,%s,'merge') returning id",
        (TEAM_A, repo_id),
    ).fetchone()[0]
    entry_id = admin.execute(
        "insert into public.memory_entries (team_id) values (%s) returning id",
        (TEAM_A,),
    ).fetchone()[0]
    version_id = admin.execute(
        "insert into public.memory_versions (entry_id, team_id, fact, change_type)"
        " values (%s,%s,'we merged the auth rewrite','added') returning id",
        (entry_id, TEAM_A),
    ).fetchone()[0]
    admin.execute(
        "insert into public.memory_citations (version_id, source_kind, source_id,"
        " excerpt) values (%s,'github',%s,'Merge pull request #12')",
        (version_id, activity_id),
    )
