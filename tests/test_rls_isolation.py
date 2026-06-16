"""RLS isolation checks, driven through the real db harness.

Worker-role assertions connect as the actual `comrade_agent` / `comrade_pipeline`
login roles (via shared.db). End-user assertions use the admin connection with
`set role authenticated` + a JWT-claims GUC to impersonate a specific user.

Requires the local Supabase stack running and scripts/setup_local_roles.sql applied.
"""
import pytest
from psycopg import errors

from shared.db import Role, team_session
from tests._seed import (
    A1, A2, ENTRY_A, TEAM_A, TEAM_B, VER_A, as_user, count as _count,
)


# ---------- end-user (authenticated) boundaries ----------

def test_member_sees_only_own_team(seeded):
    with as_user(A2) as conn:
        assert _count(conn, "select count(*) from public.teams") == 1
        assert _count(conn, "select count(*) from public.teams where id=%s", (TEAM_B,)) == 0


def test_private_thread_is_owner_only(seeded):
    with as_user(A2) as conn:  # not the owner
        assert _count(conn, "select count(*) from public.messages where thread_type='private'") == 0
    with as_user(A1) as conn:  # owner
        assert _count(conn, "select count(*) from public.messages where thread_type='private'") == 1


def test_member_cannot_author_memory(seeded):
    with pytest.raises(errors.InsufficientPrivilege):
        with as_user(A1) as conn:
            conn.execute(
                "insert into public.memory_versions (entry_id, team_id, fact,"
                " change_type) values (%s, %s, 'injected', 'added')",
                (ENTRY_A, TEAM_A),
            )


def test_member_can_trigger_revert(seeded):
    with as_user(A1) as conn:
        conn.execute(
            "insert into public.memory_reverts (entry_id, team_id, member_id,"
            " reverted_version_id) values (%s, %s, %s, %s)",
            (ENTRY_A, TEAM_A, A1, VER_A),
        )  # no error == allowed


def test_jobs_invisible_to_users(seeded):
    with as_user(A1) as conn:
        assert _count(conn, "select count(*) from public.jobs") == 0


# ---------- worker-role boundaries (real login roles) ----------

def test_agent_reads_memory_but_cannot_write(seeded):
    with team_session(Role.AGENT, TEAM_A) as conn:
        assert _count(conn, "select count(*) from public.memory_versions") == 1
    with pytest.raises(errors.InsufficientPrivilege):
        with team_session(Role.AGENT, TEAM_A) as conn:
            conn.execute(
                "insert into public.memory_versions (entry_id, team_id, fact,"
                " change_type) values (%s, %s, 'agent', 'added')",
                (ENTRY_A, TEAM_A),
            )


def test_agent_scoped_cannot_see_other_team(seeded):
    with team_session(Role.AGENT, TEAM_A) as conn:
        assert _count(conn, "select count(*) from public.teams where id=%s", (TEAM_B,)) == 0


def test_pipeline_can_compile_memory(seeded):
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        conn.execute(
            "insert into public.memory_versions (entry_id, team_id, fact,"
            " change_type) values (%s, %s, 'compiled', 'revised')",
            (ENTRY_A, TEAM_A),
        )  # no error == allowed


def test_pipeline_scoped_cannot_write_other_team(seeded):
    with pytest.raises(errors.InsufficientPrivilege):
        with team_session(Role.PIPELINE, TEAM_A) as conn:
            conn.execute(
                "insert into public.memory_entries (team_id) values (%s)", (TEAM_B,)
            )
