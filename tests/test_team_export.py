"""Taking your team's history with you.

§23.3 says memory and history are retained rather than held hostage. This is
the endpoint that makes that promise keepable, and the thing to do BEFORE you
leave — once you have left, RLS returns nothing and there is nothing left to
export.

These call the handler directly rather than through TestClient, because the
only interesting property here is the scoping, and the scoping is RLS running
as the caller. Routing is covered in test_server_auth.
"""
import json

import psycopg
import pytest

from server.app import team_export
from shared.config import settings
from tests._seed import A1, A2, B1, TEAM_A, TEAM_B


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def _export(user_id, team_id=TEAM_A):
    return json.loads(team_export(team_id, user_id).body)


def test_the_export_carries_the_team(seeded):
    doc = _export(A1)
    assert doc["team"][0]["name"] == "Team A"
    assert {m["display_name"] for m in doc["members"]} == {"A1", "A2"}
    assert any("hello team A" in m["body"] for m in doc["messages"])


def test_your_own_private_thread_comes_with_you(seeded):
    """It is yours. Leaving should not mean losing what you told Comrade."""
    doc = _export(A1)
    assert any("A1 private note" in m["body"] for m in doc["messages"])


def test_a_teammates_private_thread_does_not(seeded):
    """A2 exports the same team and gets a different document.

    Nothing in the endpoint filters this — the SELECT simply returns fewer rows
    for A2, because au_messages_select was already the answer. That is the
    property worth pinning: the export cannot over-share without RLS itself
    being wrong, and tests/test_product_read_paths.py is what guards that.
    """
    doc = _export(A2)
    assert not any("A1 private note" in m["body"] for m in doc["messages"])


def test_export_contains_only_visible_thread_metadata_and_content(seeded, admin):
    visible = admin.execute(
        "insert into public.threads (team_id, title, visibility, kind, created_by)"
        " values (%s,'Release notes','team','discussion',%s) returning id",
        (TEAM_A, A1),
    ).fetchone()[0]
    hidden = admin.execute(
        "insert into public.threads (team_id, title, visibility, kind, owner_id, created_by)"
        " values (%s,'A2 notes','restricted','discussion',%s,%s) returning id",
        (TEAM_A, A2, A2),
    ).fetchone()[0]
    admin.execute(
        "insert into public.thread_participants (thread_id, team_id, user_id, added_by)"
        " values (%s,%s,%s,%s)",
        (hidden, TEAM_A, A2, A2),
    )
    admin.execute(
        "insert into public.messages (team_id, thread_id, sender_kind, sender_id, body)"
        " values (%s,%s,'user',%s,'team-visible export')",
        (TEAM_A, visible, A1),
    )
    admin.execute(
        "insert into public.messages (team_id, thread_id, sender_kind, sender_id, body)"
        " values (%s,%s,'user',%s,'A2 export secret')",
        (TEAM_A, hidden, A2),
    )

    doc = _export(A1)

    assert str(visible) in {thread["id"] for thread in doc["threads"]}
    assert str(hidden) not in {thread["id"] for thread in doc["threads"]}
    assert any("team-visible export" in message["body"] for message in doc["messages"])
    assert not any("A2 export secret" in message["body"] for message in doc["messages"])


def test_a_stranger_gets_nothing(seeded):
    """require_membership refuses before RLS ever has to."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _export(B1)
    assert exc.value.status_code == 403


def test_a_member_cannot_export_a_team_they_left(seeded):
    from fastapi import HTTPException

    with psycopg.connect(settings.comrade_db_url_admin) as conn:
        conn.execute(
            "update public.memberships set status='left', left_at=now()"
            " where team_id=%s and user_id=%s",
            (TEAM_A, A2),
        )
        conn.commit()
    with pytest.raises(HTTPException) as exc:
        _export(A2)
    assert exc.value.status_code == 403


def test_the_export_never_reaches_another_team(seeded, admin):
    """Every relation is scoped by team_id, but the join through
    memory_versions and the one through profiles are the two that could drift."""
    admin.execute(
        "insert into public.memory_entries (id, team_id) values"
        " ('e0000000-0000-0000-0000-0000000000b1', %s)",
        (TEAM_B,),
    )
    admin.execute(
        "insert into public.memory_versions (entry_id, team_id, fact, change_type)"
        " values ('e0000000-0000-0000-0000-0000000000b1', %s,"
        " 'team B secret', 'added')",
        (TEAM_B,),
    )
    doc = _export(A1)
    blob = json.dumps(doc)
    assert "team B secret" not in blob
    assert TEAM_B not in blob
    assert str(B1) not in blob


def test_every_relation_the_client_reads_is_in_the_export(seeded):
    """An export that quietly omits a screen's data is worse than no export —
    it looks complete. Keep this in step with TEAM_SCOPED in
    tests/test_product_read_paths.py."""
    doc = _export(A1)
    for key in (
        "team", "members", "threads", "thread_participants", "messages", "tasks", "milestones", "documents",
        "wiki_pages", "wiki_facts", "wiki_citations", "change_log",
        "github_repos", "github_activity", "consent_queue",
    ):
        assert key in doc, f"{key} missing from the export"


def test_it_is_offered_as_a_file(seeded):
    resp = team_export(TEAM_A, A1)
    assert resp.media_type == "application/json"
    assert TEAM_A in resp.headers["content-disposition"]
    assert "attachment" in resp.headers["content-disposition"]
