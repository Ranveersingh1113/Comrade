"""Views are not covered by row-level security. These two were the hole.

🔴 Found by the read-path audit, 2026-08-31.

RLS protects TABLES. A view has no policies of its own, and by default runs
with the privileges of its OWNER — `security_invoker` is off unless you say
otherwise. So a view over RLS-protected tables, owned by a superuser and
granted to `authenticated`, is a hole straight through the entire model: the
underlying policies never run.

`contribution_v` is exactly that. It reads memberships, tasks, github_activity
and messages, and the product reads it on every group-room render. A member of
one team could select every team's rows: user ids, task counts, github event
counts, message counts, activity recency. Not message CONTENT, but the roster
and work-shape of every team on the instance.

`document_opens_summary` LOOKS like the same bug and is not. It runs as its
owner on purpose, because it must aggregate `document_opens` rows the caller
may not read individually — that is how it answers "opened by 3 of 4" without
naming who. Its boundary is the `is_team_member` in its own WHERE clause. My
first fix forced security_invoker=on across both and broke it; an existing
test caught that within one run.

So the rule is not "every view runs as its caller". It is: a view over
RLS-protected tables must EITHER run as its caller OR scope itself.
"""
import psycopg
import pytest

from shared.config import settings
from tests._seed import A1, A2, B1, TEAM_A, TEAM_B, as_user

VIEWS = ["contribution_v", "document_opens_summary"]


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


@pytest.mark.parametrize("view", VIEWS)
def test_every_view_is_scoped_one_way_or_the_other(admin, view):
    """A view over RLS-protected tables must do ONE of two things.

    Either it runs as its caller (`security_invoker = on`), so the policies
    underneath apply; or it carries its own scoping predicate, deliberately,
    because it needs to read rows the caller cannot.

    contribution_v was NEITHER — no predicate, running as its owner — which is
    why it returned every team's rows to anyone. document_opens_summary is the
    second kind ON PURPOSE: document_opens is private per member, and this view
    exists to answer "opened by 3 of 4" without naming who, so it must
    aggregate rows the caller may not read individually. Its boundary is the
    `is_team_member(d.team_id)` in its own WHERE clause.

    My first pass forced security_invoker=on across both and broke that
    aggregate. This test now states the rule that is actually true, so the next
    person is not told a half-rule and does not repeat it.
    """
    opts = admin.execute(
        "select reloptions from pg_class where relname=%s", (view,)
    ).fetchone()[0] or []
    definition = admin.execute(
        "select pg_get_viewdef(%s::regclass, true)", (f"public.{view}",)
    ).fetchone()[0]

    runs_as_caller = "security_invoker=on" in opts
    scopes_itself = "is_team_member" in definition
    assert runs_as_caller or scopes_itself, (
        f"{view} neither runs as its caller nor scopes itself — "
        f"it can see every team's rows"
    )


def test_contribution_v_shows_only_your_own_teams(seeded, admin):
    """The leak. B1 belongs to TEAM_B and nothing else."""
    with as_user(B1) as conn:
        teams = {
            str(r[0])
            for r in conn.execute(
                "select distinct team_id from public.contribution_v"
            ).fetchall()
        }
    assert TEAM_A not in teams, "another team's contribution rows are visible"
    assert teams <= {TEAM_B}, f"unexpected teams visible: {teams}"


def test_contribution_v_still_works_for_your_own_team(seeded):
    """The view is read on every group-room render; scoping must not empty it."""
    with as_user(A1) as conn:
        rows = conn.execute(
            "select user_id from public.contribution_v where team_id=%s", (TEAM_A,)
        ).fetchall()
    assert {str(r[0]) for r in rows} == {A1, A2}


def test_document_opens_summary_shows_only_your_own_teams(seeded, admin):
    doc_id = admin.execute(
        "insert into public.documents (team_id, kind, filename)"
        " values (%s,'text','a.txt') returning id",
        (TEAM_A,),
    ).fetchone()[0]
    admin.execute(
        "insert into public.document_opens (document_id, user_id, first_opened_at)"
        " values (%s,%s, now())",
        (doc_id, A1),
    )
    with as_user(B1) as conn:
        rows = conn.execute(
            "select * from public.document_opens_summary"
        ).fetchall()
    assert rows == [], "another team's document-open counts are visible"


@pytest.mark.parametrize("view", VIEWS)
def test_anon_holds_no_grant_on_a_view(admin, view):
    """Supabase grants anon full CRUD on every new relation, views included.

    A table survives that by RLS. A view has none, so on a view the grant IS
    the boundary — and PostgREST serves unauthenticated requests as `anon`.
    """
    grants = {
        r[0]
        for r in admin.execute(
            "select privilege_type from information_schema.role_table_grants"
            " where table_name=%s and grantee='anon'",
            (view,),
        ).fetchall()
    }
    assert grants == set(), f"anon can still reach {view}: {sorted(grants)}"
