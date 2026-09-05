"""Production code must never borrow the RLS-bypassing table-owner role."""
from pathlib import Path

import psycopg
import pytest

from shared.config import settings

ROOT = Path(__file__).resolve().parent.parent
_RUNTIME_DIRS = ("agent", "pipeline", "server")


def test_production_code_never_uses_the_admin_role():
    offenders = []
    for directory in _RUNTIME_DIRS:
        for path in (ROOT / directory).rglob("*.py"):
            if "Role.ADMIN" in path.read_text(encoding="utf-8"):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert not offenders, "production table-owner access: " + ", ".join(offenders)


def test_control_can_claim_queue_metadata_but_not_message_bodies():
    with psycopg.connect(settings.comrade_control_db_url) as conn:
        conn.execute("select count(*) from public.jobs").fetchone()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("select body from public.messages").fetchone()


def test_control_can_manage_thread_box_metadata_but_members_cannot(seeded):
    with psycopg.connect(settings.comrade_control_db_url) as conn:
        conn.execute("select box_id, state from public.thread_boxes").fetchall()
    from tests._seed import as_user, A1
    with as_user(A1) as conn:
        # Supabase grants authenticated SELECT by default; RLS is the API and
        # must still expose no row without a member policy.
        assert conn.execute("select box_id from public.thread_boxes").fetchall() == []
