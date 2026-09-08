"""What the compiler does with an answer it cannot use.

🔴 THE DEFECT (fix.md F12). `validate_decisions` degraded anything malformed —
a missing decision, an unknown action, an `entry_id` on no page — to
`action='add'`. So the compiler's response to output it could not understand
was to PUBLISH, and it did so ahead of the apply loop's own rejection, which
therefore never saw the cases it was written for.

Worse at the top end: an unreadable response produced no decisions at all, so
every extracted claim became a new fact, and the compilation row then advanced
the capture watermark over the conversation it came from.

Degrading is right for a page KIND — a model inventing 'procedure' should not
abort a compile, and the fact still belongs on an ordinary page. It is wrong
for the decision about whether to publish.
"""
import psycopg
import pytest

from pipeline import compiler
from pipeline.compiler import REJECT, Candidate, Decision, validate_decisions
from shared.config import settings
from tests._seed import A1, TEAM_A


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _candidates(n: int = 1) -> list[Candidate]:
    return [Candidate(text=f"fact {i}", excerpt=f"fact {i}") for i in range(n)]


#: Shaped as `all_active_pages` returns them — `build_consolidation_prompt`
#: renders these into the prompt, so a fact needs its text.
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


# ---------------------------------------------------------------------------
# validate_decisions
# ---------------------------------------------------------------------------

def test_a_missing_decision_is_rejected_not_published():
    """🔴 It became an `add`. The model said nothing about this candidate and
    the compiler published it."""
    out = validate_decisions(_candidates(1), PAGES, [])

    assert [d.action for d in out] == [REJECT]


def test_an_unknown_action_is_rejected_not_published():
    """🔴 Also an `add`. A word the compiler does not recognise is not a
    licence to write to the wiki."""
    out = validate_decisions(
        _candidates(1), PAGES,
        [Decision(candidate_index=0, action="publish_immediately")],
    )

    assert [d.action for d in out] == [REJECT]


def test_a_revision_of_something_that_does_not_exist_is_rejected():
    """🔴 And this one became an `add`, so a hallucinated entry id turned into
    a brand new fact — the apply loop's own rejection never saw it, because
    the action had already been rewritten."""
    out = validate_decisions(
        _candidates(1), PAGES,
        [Decision(candidate_index=0, action="revise",
                  entry_id="99999999-9999-9999-9999-999999999999")],
    )

    assert [d.action for d in out] == [REJECT]


def test_a_good_decision_is_left_alone():
    out = validate_decisions(
        _candidates(1), PAGES,
        [Decision(candidate_index=0, action="add", page_title="Operations")],
    )

    assert [d.action for d in out] == ["add"]


def test_only_the_bad_ones_are_rejected():
    """Partial results: a response that is right about one candidate and
    wrong about another must not lose the good one, and must not publish the
    bad one."""
    out = validate_decisions(
        _candidates(2), PAGES,
        [Decision(candidate_index=0, action="add", page_title="Operations"),
         Decision(candidate_index=1, action="nonsense")],
    )

    assert [d.action for d in out] == ["add", REJECT]


def test_an_invented_page_kind_still_degrades_rather_than_rejects():
    """The distinction the fix rests on. A model inventing a page kind is not
    a reason to throw away a fact — that field has a sane default and the
    claim is still true."""
    out = validate_decisions(
        _candidates(1), PAGES,
        [Decision(candidate_index=0, action="add", page_title="Operations",
                  page_kind="procedure-ish")],
    )

    assert out[0].action == "add"


# ---------------------------------------------------------------------------
# The apply loop
# ---------------------------------------------------------------------------

def test_a_rejected_decision_publishes_nothing_and_is_counted(seeded):
    conn = _admin()
    try:
        before = conn.execute(
            "select count(*) from public.memory_entries where team_id=%s",
            (TEAM_A,),
        ).fetchone()[0]
    finally:
        conn.close()

    from shared.db import Role, team_session

    with team_session(Role.PIPELINE, TEAM_A) as conn:
        result = compiler.apply_compilation(
            conn, TEAM_A, _candidates(1),
            [Decision(candidate_index=0, action=REJECT)], [None],
        )

    conn = _admin()
    try:
        after = conn.execute(
            "select count(*) from public.memory_entries where team_id=%s",
            (TEAM_A,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert after == before
    assert result["rejected"] == 1


# ---------------------------------------------------------------------------
# An answer that could not be read at all
# ---------------------------------------------------------------------------

def test_an_unreadable_answer_is_a_failure_not_an_empty_result(monkeypatch):
    """🔴 `if parsed else []` made "could not be read" and "read it, decided
    nothing" the same thing — and the first then published everything and
    advanced the watermark past the conversation."""
    class _Resp:
        parsed = None

    monkeypatch.setattr(compiler, "_get_client", lambda: _Client(_Resp()))

    with pytest.raises(compiler.ConsolidationUnavailable):
        compiler.consolidate(_candidates(1), PAGES, 100)


def test_an_answer_usable_about_nothing_is_also_a_failure(monkeypatch):
    """Parsed, and wrong about every candidate. Rejecting them all would be a
    visible record — and would advance the capture watermark past a
    conversation nothing was learned from."""
    class _Parsed:
        decisions = [Decision(candidate_index=0, action="nonsense")]

    class _Resp:
        parsed = _Parsed()

    monkeypatch.setattr(compiler, "_get_client", lambda: _Client(_Resp()))

    with pytest.raises(compiler.ConsolidationUnavailable):
        compiler.consolidate(_candidates(1), PAGES, 100)


def test_a_partly_usable_answer_is_not_a_failure(monkeypatch):
    """The other side of that line: one good decision means the compile
    happened, and the bad candidate is rejected rather than the batch retried."""
    class _Parsed:
        decisions = [
            Decision(candidate_index=0, action="add", page_title="Operations"),
            Decision(candidate_index=1, action="nonsense"),
        ]

    class _Resp:
        parsed = _Parsed()

    monkeypatch.setattr(compiler, "_get_client", lambda: _Client(_Resp()))

    out = compiler.consolidate(_candidates(2), PAGES, 100)

    assert [d.action for d in out] == ["add", REJECT]


class _Client:
    """The two attribute hops `consolidate` makes on the SDK client."""

    def __init__(self, response):
        self.models = self
        self._response = response

    def generate_content(self, **_kwargs):
        return self._response
