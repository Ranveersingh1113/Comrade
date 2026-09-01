"""Bounding what stage 2 sends, without quietly rewriting memory.

findings §20.3.3 / §6.3-8: consolidation sends the WHOLE wiki on every compile,
so cost is O(wiki size x compile frequency) and the only throttle is a
>=5-message debounce. At 300 facts that is fine. At 3,000, every fifth message
re-sends 3,000 facts through a Pro-class model.

The cap is easy. The trap underneath it is not, and it is why this file exists:

    A fact the model cannot SEE is a fact it cannot REVISE.

`validate_decisions` degrades any decision naming an unknown entry to 'add'.
So a naive truncation does not merely cost recall — it converts "revise this
existing fact" into "add a near-duplicate", every compile, for every fact
outside the window. The wiki fills with pairs that consolidation can no longer
merge, because the original is always the one that got cut.

So the window is SELECTED, not truncated: pages whose content the candidates
actually talk about come first, recency breaks ties, and what was dropped is
reported rather than silently disappearing.
"""
from datetime import datetime, timedelta, timezone

import pytest

from pipeline.compiler import Candidate, build_consolidation_prompt, select_pages

_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _page(title, facts, description="", updated=0):
    """Shaped exactly like pipeline.wiki.all_active_pages builds them.

    Note there is no page-level timestamp to sort on — recency has to come from
    the facts' own valid_from, which is the column §20.3.1 already annotates
    with. `updated` here is days after an arbitrary epoch.
    """
    return {
        "page_id": f"p-{title}",
        "title": title,
        "description": description,
        "facts": [
            {
                "entry_id": f"e{title}{i}",
                "text": t,
                "valid_from": _EPOCH + timedelta(days=updated),
                "source_kind": None,
            }
            for i, t in enumerate(facts)
        ],
    }


def _cands(*texts):
    return [Candidate(text=t) for t in texts]


def test_a_small_wiki_is_passed_through_untouched():
    """The cap must be invisible at pilot size — §20.3.3 says 300 facts is
    fine, and a selection running at 12 is just a way to introduce bugs."""
    pages = [_page("Deadlines", ["demo is 14 march"]), _page("People", ["riya owns backend"])]
    kept, dropped = select_pages(_cands("anything"), pages, max_facts=100)
    assert kept == pages
    assert dropped == []


def test_the_page_the_candidates_talk_about_survives_the_cut():
    """The whole point. A candidate about listings must arrive alongside the
    listings facts, or the model cannot revise them and emits an add instead —
    turning the cap into a duplicate factory."""
    # The relevant page is deliberately LAST in input order and is also the
    # oldest. Mutation-checked: with it first, this test passes even when
    # select_pages ignores the ranking entirely and walks the input — it was
    # green against a naive truncation, which is the implementation it exists
    # to forbid.
    pages = [
        _page("Trivia", ["the coffee machine is broken"], updated=9),
        _page("More trivia", ["the printer is on floor 3"], updated=8),
        _page("Listings", ["a listing expires after 2 hours"], updated=1),
    ]
    kept, dropped = select_pages(
        _cands("a listing expires after 90 minutes"), pages, max_facts=1
    )
    assert [p["title"] for p in kept] == ["Listings"]
    assert sorted(dropped) == ["More trivia", "Trivia"]


def test_recency_decides_when_nothing_overlaps():
    """With no signal from the candidates, the freshest pages are the ones a
    new fact is most likely to belong with."""
    pages = [
        _page("Old", ["something ancient"], updated=1),
        _page("New", ["something recent"], updated=9),
    ]
    kept, _ = select_pages(_cands("unrelated novel statement"), pages, max_facts=1)
    assert [p["title"] for p in kept] == ["New"]


def test_overlap_beats_recency():
    """Recency is the tie-break, not the ranking. A stale page the candidate
    is plainly about is exactly the page that needs revising."""
    pages = [
        _page("Listings", ["a listing expires after 2 hours"], updated=1),
        _page("Noise", ["unrelated chatter"], updated=9),
    ]
    kept, _ = select_pages(
        _cands("a listing expires after 90 minutes"), pages, max_facts=1
    )
    assert [p["title"] for p in kept] == ["Listings"]


def test_a_page_is_kept_whole_or_not_at_all():
    """Half a page is worse than none: the model would see two of a page's
    three facts, revise one and add a duplicate of the one it could not see —
    inside a page it CAN see, which is the most confusing possible outcome."""
    pages = [_page("Big", ["one", "two", "three"]), _page("Small", ["four"])]
    kept, dropped = select_pages(_cands("four"), pages, max_facts=2)
    assert [p["title"] for p in kept] == ["Small"]
    assert [len(p["facts"]) for p in kept] == [1]
    assert dropped == ["Big"]


def test_the_highest_ranked_page_survives_even_if_it_busts_the_budget():
    """Otherwise a wiki whose first page is larger than the cap sends NOTHING,
    and every candidate becomes an add. An over-budget prompt is a cost
    problem; an empty one is a correctness problem."""
    pages = [_page("Huge", [f"fact {i}" for i in range(10)])]
    kept, dropped = select_pages(_cands("fact 3"), pages, max_facts=2)
    assert [p["title"] for p in kept] == ["Huge"]
    assert dropped == []


def test_what_was_dropped_is_reported():
    """§20.3.3 is a cost argument, but a silent cap is a correctness one: the
    caller has to be able to say in the log which pages the model never saw."""
    pages = [
        _page("Keep", ["listing expires"]),
        _page("Cut me", ["irrelevant"]),
    ]
    _, dropped = select_pages(_cands("listing expires"), pages, max_facts=1)
    assert dropped == ["Cut me"]


def test_the_prompt_only_ever_contains_pages_that_survived():
    """The consistency that keeps the cap safe.

    validate_decisions builds its allowed-entry set from the SAME page list, so
    the model can only name entries it was shown. Pass the full list to one and
    the capped list to the other and a revise would be accepted against a fact
    that was never in the prompt.
    """
    pages = [
        _page("Listings", ["a listing expires after 2 hours"]),
        _page("Secret", ["should not appear"]),
    ]
    kept, _ = select_pages(_cands("listing expires after 90 minutes"), pages, max_facts=1)
    prompt = build_consolidation_prompt(_cands("listing expires after 90 minutes"), kept)
    assert "a listing expires after 2 hours" in prompt
    assert "should not appear" not in prompt


def test_an_empty_wiki_is_not_a_special_case():
    kept, dropped = select_pages(_cands("first fact ever"), [], max_facts=10)
    assert kept == []
    assert dropped == []


def test_common_words_do_not_decide_the_ranking():
    """Overlap on 'the' and 'is' would rank by page length, not relevance —
    the longest page would always win and the cap would be recency-blind and
    relevance-blind at once."""
    pages = [
        _page("Padding", ["the is a of and to it that this with " * 3]),
        _page("Listings", ["listing expiry"]),
    ]
    kept, _ = select_pages(_cands("the listing expiry is the thing"), pages, max_facts=1)
    assert [p["title"] for p in kept] == ["Listings"]


@pytest.mark.parametrize("bad", [0, -1])
def test_a_nonsense_budget_is_refused(bad):
    """A cap of zero sends an empty wiki and turns every candidate into an add
    — the duplicate factory, switched fully on. Fail loudly instead."""
    with pytest.raises(ValueError, match="max_facts"):
        select_pages(_cands("x"), [_page("P", ["y"])], max_facts=bad)
