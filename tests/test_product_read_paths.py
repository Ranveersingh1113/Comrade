"""Every table the product reads, swept for cross-tenant visibility.

The read half of the audit, 2026-08-31. The write half
(tests/test_product_write_paths.py) exists because two holes shipped in write
policies; this exists because the first thing the read sweep found was a third,
in a place neither of us had been looking: `contribution_v` bypassed RLS
entirely, because a VIEW has no policies and runs as its owner by default.

The sweep is deliberately mechanical. For every relation `frontend/src` reads,
seed a row belonging to TEAM_A and assert a member of TEAM_B sees none of it.
A leak is a leak whatever the mechanism — a missing policy, a view, a helper
that forgot to filter — and this catches the class rather than the instance.

Keep it in step with the client: if a screen reads a new table, it gets a row
here.
"""
import psycopg
import pytest

from shared.config import settings
from tests._seed import A1, A2, B1, ENTRY_A, TEAM_A, TEAM_B, VER_A, as_user, personal_thread


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def team_a_data(seeded, admin):
    """One row of TEAM_A's data in every relation the product reads."""
    doc_id = admin.execute(
        "insert into public.documents (team_id, uploader_id, kind, filename)"
        " values (%s,%s,'text','private-brief.txt') returning id",
        (TEAM_A, A1),
    ).fetchone()[0]
    admin.execute(
        "insert into public.document_opens (document_id, user_id, first_opened_at)"
        " values (%s,%s,now())",
        (doc_id, A1),
    )
    admin.execute(
        "insert into public.tasks (team_id, assignee_id, title, created_by_kind,"
        " created_by_id) values (%s,%s,'secret task','user',%s)",
        (TEAM_A, A1, A1),
    )
    admin.execute(
        "insert into public.milestones (team_id, title, due_at)"
        " values (%s,'secret milestone', now())",
        (TEAM_A,),
    )
    admin.execute(
        "insert into public.memory_pages (team_id, title, description)"
        " values (%s,'Secret Page','what we decided')",
        (TEAM_A,),
    )
    admin.execute(
        "insert into public.memory_compilations (team_id, trigger, status)"
        " values (%s,'on_demand','done')",
        (TEAM_A,),
    )
    admin.execute(
        "insert into public.memory_citations (version_id, source_kind, source_id,"
        " excerpt) values (%s,'document',%s,'a verbatim excerpt')",
        (VER_A, doc_id),
    )
    admin.execute(
        "insert into public.memory_reverts (entry_id, team_id, member_id,"
        " reverted_version_id) values (%s,%s,%s,%s)",
        (ENTRY_A, TEAM_A, A1, VER_A),
    )
    admin.execute(
        "insert into public.consent_queue (team_id, requesting_member_id, tool_name,"
        " tool_args, action_hash, tier) values (%s,%s,'task_create','{}','h1','T1')",
        (TEAM_A, A1),
    )
    repo_id = admin.execute(
        "insert into public.github_repos (team_id, repo_full_name)"
        " values (%s,'acme/secret') returning id",
        (TEAM_A,),
    ).fetchone()[0]
    admin.execute(
        "insert into public.github_activity (team_id, repo_id, node_type,"
        " author_github) values (%s,%s,'merge','maya')",
        (TEAM_A, repo_id),
    )
    return {"doc_id": doc_id}


# Every relation frontend/src reads, with the column that scopes it.
TEAM_SCOPED = [
    "documents", "tasks", "milestones", "memory_pages", "memory_compilations",
    "memory_entries", "memory_versions", "memory_reverts", "consent_queue",
    "messages", "change_log", "github_repos", "github_activity",
    "contribution_v",
]


@pytest.mark.parametrize("relation", TEAM_SCOPED)
def test_another_teams_rows_are_invisible(team_a_data, relation):
    """B1 belongs to TEAM_B. TEAM_A's rows must not exist for them."""
    with as_user(B1) as conn:
        n = conn.execute(
            f"select count(*) from public.{relation} where team_id = %s", (TEAM_A,)
        ).fetchone()[0]
    assert n == 0, f"{relation} leaked {n} of another team's rows"


def test_memory_citations_are_scoped_through_their_version(team_a_data):
    """memory_citations has no team_id — it is gated through memory_versions.

    Worth its own case: a policy that reaches through another table is exactly
    where a scope gets dropped, and the excerpt column holds verbatim source
    text from the other team's documents.
    """
    with as_user(B1) as conn:
        n = conn.execute("select count(*) from public.memory_citations").fetchone()[0]
    assert n == 0


def test_document_opens_are_private_to_the_opener(team_a_data):
    """Who has read what is a per-member fact, not a team-wide one."""
    with as_user(B1) as conn:
        assert conn.execute(
            "select count(*) from public.document_opens"
        ).fetchone()[0] == 0
    with as_user(A2) as conn:
        assert conn.execute(
            "select count(*) from public.document_opens"
        ).fetchone()[0] == 0, "a teammate's open record is visible"


def test_another_teams_membership_roster_is_invisible(team_a_data):
    """B1 sees their own rows anywhere, and TEAM_A's roster nowhere."""
    with as_user(B1) as conn:
        rows = conn.execute(
            "select team_id, user_id from public.memberships"
        ).fetchall()
    assert all(str(u) == B1 or str(t) == TEAM_B for t, u in rows), rows
    assert not any(str(t) == TEAM_A for t, _ in rows)


def test_another_teams_name_is_invisible(team_a_data):
    with as_user(B1) as conn:
        names = {r[0] for r in conn.execute("select name from public.teams").fetchall()}
    assert "Team A" not in names


def test_a_stranger_is_not_in_your_profile_directory(team_a_data):
    """profiles is gated by shares_team, not by team_id."""
    with as_user(B1) as conn:
        ids = {
            str(r[0]) for r in conn.execute("select id from public.profiles").fetchall()
        }
    assert A1 not in ids and A2 not in ids, "a non-teammate's profile is visible"
    assert B1 in ids, "you must still see yourself"


def test_a_teammates_private_thread_stays_private(team_a_data, admin):
    """The invariant the whole product rests on, asserted from the read side."""
    admin.execute(
        "insert into public.messages (team_id, thread_id,"
        " sender_kind, sender_id, body)"
        " values (%s,%s,'user',%s,'A1 confided this')",
        (TEAM_A, personal_thread(admin, TEAM_A, A1), A1),
    )
    with as_user(A2) as conn:
        bodies = [
            r[0]
            for r in conn.execute(
                "select m.body from public.messages m"
                " join public.threads t on t.id = m.thread_id"
                " where t.visibility = 'restricted'"
            ).fetchall()
        ]
    assert "A1 confided this" not in bodies


def test_a_teammates_consent_queue_is_theirs_alone(team_a_data):
    """A2 is in the same team and still must not see A1's pending actions."""
    with as_user(A2) as conn:
        assert conn.execute(
            "select count(*) from public.consent_queue"
        ).fetchone()[0] == 0


def test_your_own_team_is_still_fully_readable(team_a_data):
    """The sweep must not pass by making everything invisible to everyone."""
    with as_user(A1) as conn:
        assert conn.execute(
            "select count(*) from public.tasks where team_id=%s", (TEAM_A,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "select count(*) from public.documents where team_id=%s", (TEAM_A,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "select count(*) from public.memory_pages where team_id=%s", (TEAM_A,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "select count(*) from public.github_activity where team_id=%s", (TEAM_A,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "select count(*) from public.consent_queue"
        ).fetchone()[0] == 1
