"""A quoted source that does not exist.

🔴 THE DEFECT (fix.md F13). Chat extraction returns a `source_index` naming
the transcript line a claim came from. An index that is missing, or out of
range, degraded to "no citation" — and `excerpt_is_supported` then fell
through to `_source_text`, which answers None for a missing source. None means
"could not check", the quarantine fires only on False, and the claim was
published as an ACTIVE fact.

So a fabricated quote with no valid citation became team memory, and the next
consolidation then treated it as established.

Two Nones were being conflated. "There is nothing to check this against" and
"this kind of source cannot be re-read here" are different answers, and only
the second is uncertainty.
"""
import psycopg
import pytest

from pipeline import compiler
from shared.config import settings
from shared.db import Role, team_session
from tests._seed import A1, B1, TEAM_A, TEAM_B


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _message(team_id: str, sender: str, body: str) -> str:
    conn = _admin()
    try:
        thread_id = conn.execute(
            "select id from public.threads where team_id=%s and title='General'",
            (team_id,),
        ).fetchone()[0]
        return str(conn.execute(
            "insert into public.messages (team_id, thread_id, sender_kind,"
            " sender_id, body) values (%s,%s,'user',%s,%s) returning id",
            (team_id, thread_id, sender, body),
        ).fetchone()[0])
    finally:
        conn.close()


def _supported(source, excerpt: str):
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        return compiler.excerpt_is_supported(conn, TEAM_A, source, excerpt)


# ---------------------------------------------------------------------------

def test_a_claim_with_no_citation_is_unsupported(seeded):
    """🔴 It answered None — "could not check" — and the quarantine, which
    fires on False, let it through to active memory."""
    assert _supported(None, "the deploy key rotates on Fridays") is False


def test_a_real_quote_from_a_real_message_is_supported(seeded):
    """The other half: a citation that checks out has to still publish."""
    message_id = _message(TEAM_A, A1, "the deploy key rotates on Fridays")

    assert _supported(("message", message_id), "deploy key rotates") is True


def test_a_fabricated_quote_from_a_real_message_is_unsupported(seeded):
    """The citation resolves and the words are not in it."""
    message_id = _message(TEAM_A, A1, "the deploy key rotates on Fridays")

    assert _supported(("message", message_id), "the root password is hunter2") is False


def test_a_quote_from_another_teams_message_is_unsupported(seeded):
    """Scope is part of verification: a real quote from a source this team
    cannot see is not support for a fact in this team's wiki."""
    theirs = _message(TEAM_B, B1, "our deploy key rotates on Fridays")

    assert _supported(("message", theirs), "deploy key rotates") is False


def test_a_message_that_does_not_exist_is_unsupported(seeded):
    assert _supported(
        ("message", "99999999-9999-9999-9999-999999999999"), "anything",
    ) is False


def test_an_unreadable_source_kind_is_still_uncertain(seeded):
    """The distinction the fix rests on. A document's text is not stored, so
    apply genuinely cannot re-read it — that is "could not check", and it must
    not be collapsed into "unsupported" or every document fact would be
    quarantined."""
    assert _supported(("document", "d-1"), "anything") is None


# ---------------------------------------------------------------------------
# Through the apply loop
# ---------------------------------------------------------------------------

def _facts_and_quarantine() -> tuple[int, int]:
    conn = _admin()
    try:
        active = conn.execute(
            "select count(*) from public.memory_versions v"
            " join public.memory_entries e on e.id = v.entry_id"
            " where e.team_id=%s and v.is_active", (TEAM_A,),
        ).fetchone()[0]
        held = conn.execute(
            "select count(*) from public.memory_versions v"
            " join public.memory_entries e on e.id = v.entry_id"
            " where e.team_id=%s and not v.is_active", (TEAM_A,),
        ).fetchone()[0]
    finally:
        conn.close()
    return active, held


def test_an_uncited_claim_is_quarantined_not_published(seeded):
    """🔴 End to end: this is the claim that became active memory."""
    before_active, _ = _facts_and_quarantine()

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        result = compiler.apply_compilation(
            conn, TEAM_A,
            [compiler.Candidate(text="the root password is hunter2",
                                excerpt="the root password is hunter2")],
            [compiler.Decision(candidate_index=0, action="add",
                               page_title="Operations")],
            [None],                      # no citation
        )

    after_active, held = _facts_and_quarantine()
    assert after_active == before_active, "an uncited claim became active memory"
    assert result["quarantined"] == 1
    assert held >= 1, "and it was not written down for review either"
