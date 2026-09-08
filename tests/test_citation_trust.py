"""What has to be true before a candidate becomes a fact in the wiki.

🔴 THE DEFECTS.

An excerpt was written into `memory_citations` exactly as the model produced
it, with nothing checking that it appears in the source. A generated quote
became evidence by itself — and a citation is the one thing a member looks at
to decide whether to believe a fact.

A decision naming an entry that does not exist, or belongs to another team,
fell through `if valid is None: action = "add"`. A hallucinated id did not fail
and did not get rejected: it got PUBLISHED, as a brand new fact.

And a revision superseded whatever version happened to be active at apply
time, not the one consolidation actually read. Two compiles touching the same
entry meant the second silently overwrote the first's judgement using stale
context.
"""
import psycopg
import pytest

from pipeline.compiler import Candidate, Decision, apply_compilation
from shared.config import settings
from shared.db import Role, team_session
from tests._seed import A1, A2, TEAM_A, TEAM_B


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _message(cur, body, team_id=TEAM_A):
    thread_id = cur.execute(
        "select id from public.threads where team_id=%s and title='General'",
        (team_id,),
    ).fetchone()[0]
    sender = A1 if team_id == TEAM_A else None
    return str(cur.execute(
        "insert into public.messages (team_id, thread_id, sender_kind, sender_id,"
        " body) values (%s,%s,'user',%s,%s) returning id",
        (team_id, thread_id, sender or A1, body),
    ).fetchone()[0])


def _versions(entry_or_none=None):
    conn = _admin()
    try:
        return conn.execute(
            "select v.fact, v.is_active, v.trust from public.memory_versions v"
            " where v.team_id=%s order by v.created_at",
            (TEAM_A,),
        ).fetchall()
    finally:
        conn.close()


def _citations():
    conn = _admin()
    try:
        return conn.execute(
            "select c.excerpt from public.memory_citations c"
            " join public.memory_versions v on v.id = c.version_id"
            " where v.team_id=%s",
            (TEAM_A,),
        ).fetchall()
    finally:
        conn.close()


def _clear():
    conn = _admin()
    try:
        conn.execute("delete from public.memory_compilations where team_id=%s", (TEAM_A,))
        conn.execute("delete from public.memory_entries where team_id=%s", (TEAM_A,))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# An excerpt has to be in the source
# ---------------------------------------------------------------------------

def test_a_quote_the_source_does_not_contain_is_not_published(seeded):
    """🔴 It was written straight into memory_citations. A generated quote
    became evidence by itself, and the citation is the one thing a member
    looks at to decide whether to believe a fact."""
    _clear()
    conn = _admin()
    try:
        message_id = _message(conn, "the deadline moved to Friday")
    finally:
        conn.close()

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        result = apply_compilation(
            conn, TEAM_A,
            [Candidate(text="The budget was doubled",
                       excerpt="the budget was doubled")],
            [Decision(candidate_index=0, action="add", page_title="Project")],
            [("message", message_id)],
        )

    assert result["quarantined"] == 1
    assert result["added"] == 0
    facts = _versions()
    assert [(f, active, trust) for f, active, trust in facts] == [
        ("The budget was doubled", False, "proposed"),
    ]
    assert _citations() == [], "an unverified quote is not a citation"


def test_a_quote_that_is_in_the_source_is_published_and_cited(seeded):
    _clear()
    conn = _admin()
    try:
        message_id = _message(conn, "the deadline moved to Friday")
    finally:
        conn.close()

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        result = apply_compilation(
            conn, TEAM_A,
            [Candidate(text="The deadline is Friday",
                       excerpt="deadline moved to Friday")],
            [Decision(candidate_index=0, action="add", page_title="Project")],
            [("message", message_id)],
        )

    assert result["added"] == 1 and result["quarantined"] == 0
    assert _versions() == [("The deadline is Friday", True, "observed")]
    assert _citations() == [("deadline moved to Friday",)]


def test_matching_ignores_punctuation_and_case_the_model_changed(seeded):
    """The model re-punctuates. Requiring a byte-identical quote would
    quarantine facts that ARE supported, which teaches everyone to ignore the
    quarantine."""
    _clear()
    conn = _admin()
    try:
        message_id = _message(conn, "The deadline, finally, moved to Friday!")
    finally:
        conn.close()

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        result = apply_compilation(
            conn, TEAM_A,
            [Candidate(text="The deadline is Friday", excerpt="moved to friday")],
            [Decision(candidate_index=0, action="add", page_title="Project")],
            [("message", message_id)],
        )

    assert result["added"] == 1


def test_a_source_from_another_team_is_not_evidence(seeded):
    """Scope is part of verification: a real quote from a source this team
    cannot see is not support for a fact in this team's wiki."""
    _clear()
    conn = _admin()
    try:
        theirs = _message(conn, "their private deadline is Friday", team_id=TEAM_B)
    finally:
        conn.close()

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        result = apply_compilation(
            conn, TEAM_A,
            [Candidate(text="The deadline is Friday",
                       excerpt="their private deadline is Friday")],
            [Decision(candidate_index=0, action="add", page_title="Project")],
            [("message", theirs)],
        )

    assert result["quarantined"] == 1
    assert _citations() == []


# ---------------------------------------------------------------------------
# A malformed decision is rejected, never silently published
# ---------------------------------------------------------------------------

def test_a_decision_naming_an_entry_that_does_not_exist_is_rejected(seeded):
    """🔴 `if valid is None: action = "add"`. A hallucinated entry id did not
    fail and did not get rejected — it got PUBLISHED as a brand new fact."""
    _clear()
    conn = _admin()
    try:
        message_id = _message(conn, "the deadline moved to Friday")
    finally:
        conn.close()

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        result = apply_compilation(
            conn, TEAM_A,
            [Candidate(text="The deadline is Friday",
                       excerpt="deadline moved to Friday")],
            [Decision(candidate_index=0, action="revise",
                      entry_id="11111111-1111-1111-1111-111111111111")],
            [("message", message_id)],
        )

    assert result["rejected"] == 1
    assert result["added"] == 0
    assert _versions() == []


# ---------------------------------------------------------------------------
# A revision binds to the version consolidation read
# ---------------------------------------------------------------------------

def test_revising_a_version_that_moved_under_us_is_refused(seeded):
    """🔴 The update superseded whatever was active at APPLY time, not what
    consolidation read. Two compiles on one entry meant the second silently
    overwrote the first's judgement from stale context."""
    from pipeline.compiler import StaleConsolidation

    _clear()
    conn = _admin()
    try:
        message_id = _message(conn, "the deadline moved to Friday")
    finally:
        conn.close()

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        apply_compilation(
            conn, TEAM_A,
            [Candidate(text="The deadline is Friday",
                       excerpt="deadline moved to Friday")],
            [Decision(candidate_index=0, action="add", page_title="Project")],
            [("message", message_id)],
        )
    conn = _admin()
    try:
        entry_id, version_id = conn.execute(
            "select entry_id, id from public.memory_versions"
            " where team_id=%s and is_active", (TEAM_A,),
        ).fetchone()
    finally:
        conn.close()

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        with pytest.raises(StaleConsolidation):
            apply_compilation(
                conn, TEAM_A,
                [Candidate(text="The deadline is Monday",
                           excerpt="deadline moved to Friday")],
                [Decision(candidate_index=0, action="revise", entry_id=str(entry_id),
                          seen_version_id="22222222-2222-2222-2222-222222222222")],
                [("message", message_id)],
            )


def test_revising_the_version_consolidation_read_succeeds(seeded):
    _clear()
    conn = _admin()
    try:
        message_id = _message(conn, "the deadline moved to Friday")
    finally:
        conn.close()

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        apply_compilation(
            conn, TEAM_A,
            [Candidate(text="The deadline is Friday",
                       excerpt="deadline moved to Friday")],
            [Decision(candidate_index=0, action="add", page_title="Project")],
            [("message", message_id)],
        )
    conn = _admin()
    try:
        entry_id, version_id = conn.execute(
            "select entry_id, id from public.memory_versions"
            " where team_id=%s and is_active", (TEAM_A,),
        ).fetchone()
    finally:
        conn.close()

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        result = apply_compilation(
            conn, TEAM_A,
            [Candidate(text="The deadline is Monday",
                       excerpt="deadline moved to Friday")],
            [Decision(candidate_index=0, action="revise", entry_id=str(entry_id),
                      seen_version_id=str(version_id))],
            [("message", message_id)],
        )

    assert result["revised"] == 1


def test_a_revision_bound_to_the_current_version_works(seeded):
    """The ordinary path, with the binding the real callers now supply.

    🔴 This was `test_a_revision_with_no_expectation_still_works`, and its
    docstring said a missing version "must not become an error" — which was
    the wildcard contract, and the defect (fix.md F14): a null expectation
    matched whatever was active, so a compile could supersede a version it had
    never read. `consolidate` binds every revision now, so a revision arriving
    without one is a wiring fault rather than an older payload.
    """
    _clear()
    conn = _admin()
    try:
        message_id = _message(conn, "the deadline moved to Friday")
    finally:
        conn.close()

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        apply_compilation(
            conn, TEAM_A,
            [Candidate(text="The deadline is Friday",
                       excerpt="deadline moved to Friday")],
            [Decision(candidate_index=0, action="add", page_title="Project")],
            [("message", message_id)],
        )
    conn = _admin()
    try:
        entry_id, version_id = conn.execute(
            "select entry_id, id from public.memory_versions"
            " where team_id=%s and is_active", (TEAM_A,),
        ).fetchone()
    finally:
        conn.close()

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        result = apply_compilation(
            conn, TEAM_A,
            [Candidate(text="The deadline is Monday",
                       excerpt="deadline moved to Friday")],
            [Decision(candidate_index=0, action="revise", entry_id=str(entry_id),
                      seen_version_id=str(version_id))],
            [("message", message_id)],
        )

    assert result["revised"] == 1


# ---------------------------------------------------------------------------
# What the wiki shows
# ---------------------------------------------------------------------------

def test_quarantined_facts_stay_out_of_the_consolidation_context(seeded):
    """A proposed fact must not be shown to the next compile as established,
    or the fabrication launders itself into the wiki one round later."""
    from pipeline.wiki import all_active_pages

    _clear()
    conn = _admin()
    try:
        message_id = _message(conn, "the deadline moved to Friday")
    finally:
        conn.close()

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        apply_compilation(
            conn, TEAM_A,
            [Candidate(text="The budget was doubled", excerpt="invented quote")],
            [Decision(candidate_index=0, action="add", page_title="Project")],
            [("message", message_id)],
        )
        pages = all_active_pages(conn, TEAM_A)

    facts = [f["text"] for page in pages for f in page["facts"]]
    assert "The budget was doubled" not in facts
