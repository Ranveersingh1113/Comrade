"""fix.md F65 — the invitation acceptance could delete a real person's account.

🔴 THE DEFECT. `an_invitation_is_delivered` looked for an existing account with

    select id from auth.users where email = %s

and treated "no row" as proof that the user id the invite returned afterwards
had been created by this run — then deleted that user's identities and auth
row, and reported PASS.

The two are not the same claim, and the gap is reachable two ways:

* **Case.** `public.user_id_by_email`, the resolver the invite actually uses,
  compares `lower(email) = lower(p_email)`
  (`supabase/migrations/20260719150000_invites.sql`). An authorised recipient
  typed `Existing@Example.test` misses a case-sensitive pre-check, the invite
  reports already-registered, the resolver returns the EXISTING id, and the
  cleanup deletes that account.
* **Time.** Normalising the comparison does not fix it. A signup landing
  between the lookup and the resolution produces an account this run did not
  make.

Deleting by exact id does not establish ownership of that id, and for an
address the operator chose there is no provenance to appeal to. So nothing is
deleted any more, and these tests hold that line: **no invitation path may
issue a delete against auth.** The fixture user this file creates for itself
is a different matter and is torn down as before.

No database, no email: the connection and the invite helper are doubles, which
is the only safe way to test a path whose defect was deleting real accounts.
"""
import pytest

from scripts import post_deploy_check as pdc


EXISTING_ID = "11111111-2222-4333-8444-555555555555"


class _Cursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Connection:
    """Records every statement, and answers the existence lookup."""

    def __init__(self, existing_email=None):
        self.existing_email = existing_email
        self.statements: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.statements.append((" ".join(sql.split()), params))
        lowered = sql.lower()
        if "from auth.users" in lowered and "select" in lowered:
            # The real table's semantics: GoTrue stores one row per address and
            # `user_id_by_email` matches it case-insensitively.
            asked = str(params[0])
            if (self.existing_email
                    and asked.lower() == self.existing_email.lower()
                    # ...but only a query that NORMALISES will find it.
                    and "lower(email)" in lowered):
                return _Cursor((EXISTING_ID,))
        return _Cursor(None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def invitation(monkeypatch):
    """The check wired to a recorded connection and a doubled invite."""
    state = {"conn": None, "resolved": EXISTING_ID}
    monkeypatch.setattr(pdc, "results", [])

    def admin():
        return state["conn"]

    monkeypatch.setattr(pdc, "admin", admin)
    monkeypatch.setattr("server.invites._invite_or_resolve_user",
                        lambda email: state["resolved"])
    return state


def _deletes(conn):
    return [sql for sql, _ in conn.statements
            if sql.lower().startswith("delete") and "auth." in sql.lower()]


def test_a_mixed_case_existing_address_is_never_deleted(invitation, monkeypatch):
    """🔴 THE REPORTED CASE. `Existing@Example.test` against a stored
    `existing@example.test`: the old pre-check missed it, the resolver found
    it, and the cleanup removed somebody's account."""
    monkeypatch.setenv("COMRADE_CHECK_INVITE_EMAIL", "Existing@Example.test")
    invitation["conn"] = _Connection(existing_email="existing@example.test")

    pdc.an_invitation_is_delivered()

    assert _deletes(invitation["conn"]) == [], invitation["conn"].statements
    label, ok, detail = pdc.results[-1]
    assert ok, (label, detail)
    # And it says which case this was, having asked the question the invite asks.
    assert "already registered" in detail, detail


def test_an_account_appearing_between_lookup_and_invite_is_never_deleted(
    invitation, monkeypatch,
):
    """The race the normalisation cannot close: nothing exists when the check
    looks, and the id it gets back belongs to a signup that landed in between.
    Indistinguishable from "we made it" — which is why nothing is deleted."""
    monkeypatch.setenv("COMRADE_CHECK_INVITE_EMAIL", "newcomer@example.test")
    invitation["conn"] = _Connection(existing_email=None)

    pdc.an_invitation_is_delivered()

    assert _deletes(invitation["conn"]) == [], invitation["conn"].statements
    label, ok, detail = pdc.results[-1]
    assert ok, (label, detail)


def test_the_existence_lookup_asks_the_question_the_invite_asks(invitation,
                                                                monkeypatch):
    """Normalisation is not the safety mechanism — nothing being deleted is —
    but an unnormalised lookup reports the wrong thing, and reporting the wrong
    thing about someone's account is how this started."""
    monkeypatch.setenv("COMRADE_CHECK_INVITE_EMAIL", "Existing@Example.test")
    invitation["conn"] = _Connection(existing_email="existing@example.test")

    pdc.an_invitation_is_delivered()

    lookups = [sql for sql, _ in invitation["conn"].statements
               if "from auth.users" in sql.lower()]
    assert lookups, invitation["conn"].statements
    assert all("lower(email)" in sql.lower() for sql in lookups), lookups


def test_a_rejected_key_is_reported_and_still_deletes_nothing(invitation,
                                                              monkeypatch):
    """The failure this check exists for, on the path where an exception
    unwinds: it must not skip past the no-delete rule either."""
    from fastapi import HTTPException

    monkeypatch.setenv("COMRADE_CHECK_INVITE_EMAIL", "someone@example.test")
    invitation["conn"] = _Connection(existing_email=None)

    def rejected(email):
        raise HTTPException(503, "the configured Supabase secret key was"
                                 " rejected by the project (401)")

    monkeypatch.setattr("server.invites._invite_or_resolve_user", rejected)

    pdc.an_invitation_is_delivered()

    assert _deletes(invitation["conn"]) == [], invitation["conn"].statements
    label, ok, detail = pdc.results[-1]
    assert not ok, (label, detail)
    assert "503" in detail and "rejected" in detail, detail


def test_without_a_recipient_nothing_happens_at_all(invitation, monkeypatch):
    """The default. An invitation emails a real person, so an unset recipient
    must not reach the project, the database, or this file's results."""
    monkeypatch.delenv("COMRADE_CHECK_INVITE_EMAIL", raising=False)
    invitation["conn"] = _Connection(existing_email=None)

    def forbidden(email):
        raise AssertionError("invited with no recipient configured")

    monkeypatch.setattr("server.invites._invite_or_resolve_user", forbidden)

    pdc.an_invitation_is_delivered()

    assert invitation["conn"].statements == []
    assert pdc.results == []
