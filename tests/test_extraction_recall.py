"""The recall scorer itself — pure, so it runs in the normal gate.

The live measurement is tests/test_extraction_recall_live.py. This file is
about the matcher, and specifically about the two ways a recall metric lies:
by under-reporting when the extractor merely rephrased, and by over-reporting
when a loose match credits a fact that was never found. The second is the
dangerous one — a metric built to detect extractor starvation must not hide it.
"""
import pytest

from evaluation.extraction import ExpectedFact, recall, report


def _f(text, *keys):
    return ExpectedFact(text=text, keys=keys)


def test_a_rephrased_fact_still_counts():
    """The whole reason this is not exact matching.

    Same fact, no shared prefix, different word order — exact matching would
    call this a miss and every run would report starvation that is not there.
    """
    r = recall(
        produced=["Team demo scheduled for 14 March in the main hall."],
        expected=[_f("The demo is on Friday 14 March", "demo", "14 march")],
    )
    assert r["recall"] == 1.0
    assert r["missed"] == []


def test_a_genuinely_missing_fact_is_reported_with_its_text():
    """`missed` is the actionable half — the number alone tells nobody what to
    go and look at."""
    r = recall(
        produced=["The demo is on 14 March."],
        expected=[
            _f("The demo is on Friday 14 March", "demo", "14 march"),
            _f("Priya owns the API integration", "priya", "api"),
        ],
    )
    assert r["recall"] == 0.5
    assert r["missed"] == ["Priya owns the API integration"]


def test_a_partial_key_match_is_not_a_match():
    """All keys or nothing. A produced line mentioning Priya but not the API
    is a different fact, and crediting it would report recall the extractor
    did not earn."""
    r = recall(
        produced=["Priya joined the team on Monday."],
        expected=[_f("Priya owns the API integration", "priya", "api")],
    )
    assert r["recall"] == 0.0


def test_a_key_inside_a_longer_word_is_not_a_match():
    """The failure that would quietly break this metric.

    Bare substring matching credits "demo" against "democracy" — a recall
    figure that goes up when nothing was found. Word-boundary matching is the
    difference between a metric and a comforting number.
    """
    r = recall(
        produced=["The team discussed democracy in the abstract."],
        expected=[_f("The demo is on 14 March", "demo")],
    )
    assert r["recall"] == 0.0
    assert r["missed"] == ["The demo is on 14 March"]


def test_punctuation_and_case_do_not_decide_anything():
    r = recall(
        produced=["Merged PR #12 — by Maya!"],
        expected=[_f("Maya merged pull request 12", "maya", "12")],
    )
    assert r["recall"] == 1.0


def test_multi_word_keys_match_across_the_space():
    r = recall(
        produced=["Submission is due 14 March."],
        expected=[_f("Due 14 March", "14 march")],
    )
    assert r["recall"] == 1.0


def test_extras_are_shown_but_never_scored():
    """The labeller lists what MUST be found, not everything findable.

    Penalising a fact the human did not think to label would punish the
    extractor for being thorough, and would push the metric down exactly when
    the extractor is doing well.
    """
    r = recall(
        produced=["The demo is on 14 March.", "Coffee machine is broken."],
        expected=[_f("The demo is on 14 March", "demo", "14 march")],
    )
    assert r["recall"] == 1.0
    assert r["extra"] == ["Coffee machine is broken."]


def test_one_produced_line_is_not_counted_twice():
    """Two expected facts, one produced line that satisfies both key sets.

    Recall still credits both — they were both found, in one sentence — but the
    line is consumed for the purposes of `extra`, so it does not also appear as
    unmatched noise.
    """
    r = recall(
        produced=["The demo is on 14 March and Priya owns the API."],
        expected=[
            _f("The demo is on 14 March", "demo", "14 march"),
            _f("Priya owns the API", "priya", "api"),
        ],
    )
    assert r["recall"] == 1.0
    assert r["extra"] == []


def test_an_empty_extraction_scores_zero_rather_than_erroring():
    """The exact case this metric exists to catch — stage 1 returned nothing.

    It must produce a number, not an exception: a crash in the scorer would be
    indistinguishable from a broken eval and would get the run ignored.
    """
    r = recall(produced=[], expected=[_f("The demo is on 14 March", "demo")])
    assert r["recall"] == 0.0
    assert r["missed"] == ["The demo is on 14 March"]


def test_an_empty_expected_set_is_refused():
    """0/0 is not 100%. A document nobody labelled is a labelling bug, and
    silently scoring it perfect would let the held-out set rot unnoticed."""
    with pytest.raises(ValueError, match="meaningless"):
        recall(produced=["anything"], expected=[])


def test_the_report_names_what_was_missed():
    text = report({
        "brief.txt": recall(
            produced=["The demo is on 14 March."],
            expected=[
                _f("The demo is on 14 March", "demo", "14 march"),
                _f("Priya owns the API", "priya", "api"),
            ],
        ),
    })
    assert "brief.txt: 1/2 (50%)" in text
    assert "MISSED  Priya owns the API" in text
    assert "OVERALL stage-1 recall: 1/2 (50%)" in text


def test_a_key_may_offer_interchangeable_forms():
    """Measured, not hypothetical.

    The first live run scored "Authentication feature is complete and merged"
    as a MISS against the key `auth` — whole-token matching working exactly as
    designed, and reporting starvation that had not happened. Loosening the
    matcher to prefixes would fix it and bring back `demo` matching
    `democracy`; naming both forms fixes only the case a human looked at.
    """
    r = recall(
        produced=["Authentication feature is complete and merged."],
        expected=[_f("Auth is done and merged", ("auth", "authentication"))],
    )
    assert r["recall"] == 1.0


def test_alternatives_do_not_weaken_the_other_keys():
    """All keys still required — a tuple relaxes ONE key's spelling, not the
    conjunction. Otherwise the escape hatch quietly becomes an any-match."""
    r = recall(
        produced=["Authentication is complete."],
        expected=[_f("Riya finished auth", ("auth", "authentication"), "riya")],
    )
    assert r["recall"] == 0.0
