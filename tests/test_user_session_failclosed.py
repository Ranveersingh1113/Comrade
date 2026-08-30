"""user_session must never silently fall back to a BYPASSRLS connection.

`shared/db.py` fell back to the ADMIN url when COMRADE_AUTHENTICATOR_DB_URL was
unset. ADMIN is the table owner and bypasses RLS, so a production deployment
that forgot the variable would silently run every member query with no row
security at all — and, since the connection pool landed, share a pool with the
control plane too.

Failing closed turns a silent security downgrade into a startup error. Found by
the Phase 0 whole-branch review.
"""
import pytest

from shared import db


def test_user_session_refuses_to_fall_back_to_admin(monkeypatch):
    monkeypatch.setattr(db.settings, "comrade_authenticator_db_url", "")
    with pytest.raises(RuntimeError, match="COMRADE_AUTHENTICATOR_DB_URL"):
        with db.user_session("00000000-0000-0000-0000-000000000001"):
            pass


def test_user_session_still_works_when_configured(seeded):
    """The guard must not break the configured path."""
    from tests._seed import A1

    with db.user_session(A1) as conn:
        assert conn.execute("select current_user").fetchone()[0] == "authenticated"
