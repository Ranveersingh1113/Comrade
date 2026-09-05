"""Schema assertions for the Phase 1 cheap-columns migration.

findings §3.1 listed these as missing; §23.4-3 promoted the token/cost pair from
housekeeping to the billing input; §25.7 asked for `parent_run_id` to be taken
inside a migration that was happening anyway, because it is painful to retrofit.
"""
import psycopg
import pytest

from shared.config import settings
from tests._seed import TEAM_A


@pytest.fixture
def admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def _columns(admin, table):
    return {
        r[0]: r[1]
        for r in admin.execute(
            "select column_name, data_type from information_schema.columns"
            " where table_schema='public' and table_name=%s",
            (table,),
        ).fetchall()
    }


def test_agent_runs_has_cost_and_lineage_columns(admin):
    cols = _columns(admin, "agent_runs")
    assert cols.get("input_tokens") == "integer"
    assert cols.get("output_tokens") == "integer"
    assert cols.get("cost_usd") == "numeric"
    assert cols.get("parent_run_id") == "uuid"


def test_consent_queue_has_thread_trace_and_reason_columns(admin):
    cols = _columns(admin, "consent_queue")
    assert "batch_id" not in cols
    assert cols.get("thread_id") == "uuid"
    assert cols.get("agent_run_id") == "uuid"
    assert cols.get("resolution_reason") == "text"


def test_agent_runs_accepts_cancelled_status(seeded, admin):
    admin.execute(
        "insert into public.agent_runs (team_id, trigger_type, status)"
        " values (%s,'user','cancelled')",
        (TEAM_A,),
    )


def test_agent_runs_accepts_the_agent_trigger_type(seeded, admin):
    """§25.7 keeps the multi-agent door open without walking through it."""
    admin.execute(
        "insert into public.agent_runs (team_id, trigger_type) values (%s,'agent')",
        (TEAM_A,),
    )


def test_agent_runs_still_rejects_an_unknown_status(seeded, admin):
    """Widening the check must not have removed it."""
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute(
            "insert into public.agent_runs (team_id, trigger_type, status)"
            " values (%s,'user','banana')",
            (TEAM_A,),
        )


def test_parent_run_id_references_a_run(seeded, admin):
    parent = admin.execute(
        "insert into public.agent_runs (team_id, trigger_type) values (%s,'user')"
        " returning id",
        (TEAM_A,),
    ).fetchone()[0]
    child = admin.execute(
        "insert into public.agent_runs (team_id, trigger_type, parent_run_id)"
        " values (%s,'agent',%s) returning id",
        (TEAM_A, parent),
    ).fetchone()[0]
    assert child is not None

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        admin.execute(
            "insert into public.agent_runs (team_id, trigger_type, parent_run_id)"
            " values (%s,'agent','00000000-0000-0000-0000-0000000000ff')",
            (TEAM_A,),
        )
