"""A revision that cannot say what it was revising.

🔴 THE DEFECT (fix.md F14). `bind_to_seen_versions` tells each revision which
version consolidation actually read, and the supersession guard used it as
`(%s::uuid is null or id = %s::uuid)` — so a MISSING version was a WILDCARD,
matching whatever happens to be active now.

Chat and documents bound theirs. The GitHub compile did not call the function
at all. So a GitHub result that had read version A could land after a chat
compile had already replaced it with version B, and silently supersede B — a
compile erasing one it never saw, which is the exact failure the binding
exists to prevent.

Binding moved into `consolidate`, which is the function that READ the pages:
the version id is a fact about what was shown, not a decision a caller makes
afterwards, and a third call site is a third chance to forget.
"""
import psycopg
import pytest

from pipeline import compiler
from pipeline.compiler import Candidate, Decision
from shared.config import settings
from shared.db import Role, team_session
from tests._seed import A1, TEAM_A

PAGES = [{
    "title": "Operations", "id": "p1", "description": "How the team runs",
    "kind": "fact",
    "facts": [{
        "entry_id": "11111111-1111-1111-1111-111111111111",
        "version_id": "22222222-2222-2222-2222-222222222222",
        "text": "deploys happen on Tuesdays",
        "valid_from": None, "source_kind": None,
    }],
}]


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


class _Client:
    def __init__(self, decisions):
        self.models = self
        self._decisions = decisions

    def generate_content(self, **_kwargs):
        parsed = type("P", (), {"decisions": self._decisions})()
        return type("R", (), {"parsed": parsed})()


# ---------------------------------------------------------------------------
# The binding happens where the pages were read
# ---------------------------------------------------------------------------

def test_consolidate_binds_the_version_it_read(monkeypatch):
    """🔴 It did not — each caller was expected to remember, and the GitHub
    compile did not."""
    monkeypatch.setattr(compiler, "_get_client", lambda: _Client([
        Decision(candidate_index=0, action="revise",
                 entry_id="11111111-1111-1111-1111-111111111111"),
    ]))

    out = compiler.consolidate(
        [Candidate(text="deploys happen on Wednesdays", excerpt="Wednesdays")],
        PAGES, 100,
    )

    assert out[0].seen_version_id == "22222222-2222-2222-2222-222222222222"


def test_an_add_needs_no_binding(monkeypatch):
    """Only a revision is about an existing version."""
    monkeypatch.setattr(compiler, "_get_client", lambda: _Client([
        Decision(candidate_index=0, action="add", page_title="Operations"),
    ]))

    out = compiler.consolidate(
        [Candidate(text="a new thing", excerpt="a new thing")], PAGES, 100,
    )

    assert out[0].seen_version_id is None


def test_the_github_compile_binds_too(monkeypatch):
    """The caller that forgot. It goes through `consolidate` like the others,
    so moving the binding there is what fixes it — the point of the change is
    that no caller has to know."""
    import inspect

    from pipeline import github

    source = inspect.getsource(github.compile_github_activity)
    assert "consolidate(" in source, (
        "the GitHub compile no longer goes through consolidate; the binding"
        " has to follow it wherever it went"
    )


# ---------------------------------------------------------------------------
# And an unbound revision is refused rather than treated as a wildcard
# ---------------------------------------------------------------------------

def _cited(text: str) -> tuple[str, str]:
    """A real message to quote, so the candidate gets past the EVIDENCE check.

    F13 makes an uncited candidate unsupported, and unsupported is quarantined
    before the apply loop reaches the version binding — so a test about the
    binding has to supply a citation that checks out.
    """
    conn = _admin()
    try:
        thread_id = conn.execute(
            "select id from public.threads where team_id=%s and title='General'",
            (TEAM_A,),
        ).fetchone()[0]
        message_id = conn.execute(
            "insert into public.messages (team_id, thread_id, sender_kind,"
            " sender_id, body) values (%s,%s,'user',%s,%s) returning id",
            (TEAM_A, thread_id, A1, text),
        ).fetchone()[0]
    finally:
        conn.close()
    return "message", str(message_id)


def _entry_with_two_versions() -> tuple[str, str]:
    """An entry whose active version is NOT the one a stale compile read."""
    conn = _admin()
    try:
        page_id = conn.execute(
            "insert into public.memory_pages (team_id, title, description)"
            " values (%s,'Operations','How the team runs') returning id",
            (TEAM_A,),
        ).fetchone()[0]
        entry_id = conn.execute(
            "insert into public.memory_entries (team_id, page_id)"
            " values (%s,%s) returning id", (TEAM_A, page_id),
        ).fetchone()[0]
        comp_id = conn.execute(
            "insert into public.memory_compilations (team_id, status, trigger)"
            " values (%s,'done','scheduled') returning id", (TEAM_A,),
        ).fetchone()[0]
        old_version = conn.execute(
            "insert into public.memory_versions (entry_id, team_id,"
            " compilation_id, fact, change_type, is_active, trust) values"
            " (%s,%s,%s,'deploys happen on Tuesdays','added',false,'superseded')"
            " returning id", (entry_id, TEAM_A, comp_id),
        ).fetchone()[0]
        conn.execute(
            "insert into public.memory_versions (entry_id, team_id,"
            " compilation_id, fact, change_type, is_active, trust) values"
            " (%s,%s,%s,'deploys happen on Thursdays','revised',true,'observed')",
            (entry_id, TEAM_A, comp_id),
        )
    finally:
        conn.close()
    return str(entry_id), str(old_version)


def test_an_unbound_revision_is_rejected(seeded):
    """🔴 With a null version the guard matched whatever was active, so this
    would have superseded a newer fact it never read."""
    entry_id, _ = _entry_with_two_versions()

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        result = compiler.apply_compilation(
            conn, TEAM_A,
            [Candidate(text="deploys happen on Fridays", excerpt="Fridays")],
            [Decision(candidate_index=0, action="revise", entry_id=entry_id)],
            [_cited("deploys happen on Fridays")],
        )

    assert result["rejected"] == 1
    assert result["revised"] == 0


def test_the_newer_fact_survives(seeded):
    entry_id, _ = _entry_with_two_versions()

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        compiler.apply_compilation(
            conn, TEAM_A,
            [Candidate(text="deploys happen on Fridays", excerpt="Fridays")],
            [Decision(candidate_index=0, action="revise", entry_id=entry_id)],
            [_cited("deploys happen on Fridays")],
        )

    conn = _admin()
    try:
        active = conn.execute(
            "select fact from public.memory_versions"
            " where entry_id=%s and is_active", (entry_id,),
        ).fetchall()
    finally:
        conn.close()
    assert [row[0] for row in active] == ["deploys happen on Thursdays"]


def test_a_revision_bound_to_a_stale_version_is_still_caught(seeded):
    """The binding does not replace the staleness check, it feeds it: a
    revision naming a version that is no longer active must not apply."""
    entry_id, old_version = _entry_with_two_versions()

    with pytest.raises(compiler.StaleConsolidation):
        with team_session(Role.PIPELINE, TEAM_A) as conn:
            compiler.apply_compilation(
                conn, TEAM_A,
                [Candidate(text="deploys happen on Fridays", excerpt="Fridays")],
                [Decision(candidate_index=0, action="revise", entry_id=entry_id,
                          seen_version_id=old_version)],
                [_cited("deploys happen on Fridays")],
            )
