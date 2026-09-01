"""Stage-1 extraction recall: the metric Comrade's biggest risk had none of.

findings §20.3.2 names extractor starvation as *"Comrade's unguarded failure
mode"* and *"a larger practical risk than any retrieval-architecture
question"*. Stage 1 is a single LLM call and the only path from a source into
memory: the 2026-07-15 RAG deletion removed the net behind it, so a fact the
extractor misses is unreachable forever. And recall was **unmeasured** — no
metric, no held-out set, no regression signal.

This is the signal. Deliberately not a framework, per §20.3.2's own guidance:
a handful of documents with hand-labelled expected facts is enough to notice a
regression, and anything larger is a research project nobody will run.

WHY KEY TERMS RATHER THAN A SIMILARITY SCORE
---------------------------------------------
The extractor rephrases. "The demo is Friday 14 March" may come back as "Team
demo scheduled for 14 March" — same fact, no shared prefix — so exact matching
under-reports catastrophically, and a fuzzy threshold is a number nobody can
defend when it disagrees with a human.

So the LABEL does the work: each expected fact carries the terms that make it
that fact, and a produced fact matches if it contains all of them. The person
writing the label already knows which words are load-bearing; that judgement is
better recorded than re-derived by a matcher.

Matching is word-boundary-aware, which matters in the direction this metric
exists for. Bare substring matching would let "demo" match "democracy" and
credit a fact that was never found — a recall metric that over-reports hides
exactly the starvation it was built to detect.
"""
import re
import unicodedata
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ExpectedFact:
    """One fact a human says this source contains.

    `text` is for the report — it is what a reader sees in the "missed" list.
    `keys` is what actually decides the match: the terms without which the
    produced line is not this fact. Keep them few and essential; a key list
    that restates the sentence is just exact matching with extra steps.

    A key may be a STRING, or a TUPLE of interchangeable forms of the same
    term, any one of which counts. That is not a loosening of the matcher, it
    is the labeller doing the job the module claims for them.

    Measured on the first run: the extractor found "Authentication feature is
    complete and merged" and the key `auth` scored it a miss, because
    whole-token matching — correctly — will not match `auth` inside
    `authentication`. Relaxing the matcher to prefixes would fix that case and
    reintroduce `demo` matching `democracy`, which is the error direction that
    makes a recall metric lie. Writing `("auth", "authentication")` fixes only
    the case a human looked at.
    """
    text: str
    keys: tuple[str | tuple[str, ...], ...]


@dataclass
class Document:
    name: str
    kind: str  # 'document' | 'chat' | 'github' — picks the extraction prompt
    body: str
    expected: tuple[ExpectedFact, ...] = field(default_factory=tuple)


def _normalise(s: str) -> str:
    """Lowercase, fold accents, collapse punctuation and whitespace to spaces.

    Punctuation becomes a space rather than nothing so "#12" and "12" both
    reduce to a token boundary — dropping it entirely would glue neighbouring
    words into a token that matches neither.
    """
    folded = unicodedata.normalize("NFKD", s)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", folded.lower()).strip()


def _contains(haystack: str, needle: str) -> bool:
    """Whole-token containment: every token of `needle`, in order, in `haystack`.

    Both are already normalised, so this is a word-boundary match without a
    regex per call — `demo` does not match `democracy`, and a multi-word key
    like `14 march` still matches across the space.
    """
    if not needle:
        return False
    return f" {needle} " in f" {haystack} "


def recall(produced: list[str], expected: list[ExpectedFact]) -> dict:
    """How much of what a human found did the extractor also find?

    Returns the fraction plus the working, because the number alone is not
    actionable: `missed` is the list to go and look at, and `found` lets a
    reader check the matcher agreed for the right reason.

    `extra` — produced facts matching no expected one — is reported and NOT
    scored. The labeller lists what must be found, not everything findable, so
    penalising extras would punish an extractor for being thorough. If extras
    ever need judging, that is a precision metric and a different labelling
    job.
    """
    if not expected:
        raise ValueError("recall over an empty expected set is meaningless")

    norm_produced = [(p, _normalise(p)) for p in produced]
    found: list[tuple[str, str]] = []
    missed: list[str] = []
    matched_produced: set[int] = set()

    for want in expected:
        # Every key must be present; a key given as a tuple is satisfied by any
        # one of its forms.
        keys = tuple(
            tuple(_normalise(form) for form in (k if isinstance(k, tuple) else (k,)))
            for k in want.keys
        )
        hit = next(
            (
                i for i, (_, np) in enumerate(norm_produced)
                if all(any(_contains(np, form) for form in alts) for alts in keys)
            ),
            None,
        )
        if hit is None:
            missed.append(want.text)
        else:
            found.append((want.text, norm_produced[hit][0]))
            matched_produced.add(hit)

    return {
        "recall": len(found) / len(expected),
        "found": found,
        "missed": missed,
        "extra": [p for i, (p, _) in enumerate(norm_produced) if i not in matched_produced],
    }


def report(results: dict[str, dict]) -> str:
    """One readable block per document, then the overall figure.

    Printed by the live test so a run leaves something a human can act on
    rather than a bare assertion.
    """
    lines: list[str] = []
    total_found = total_expected = 0
    for name, r in results.items():
        n_found = len(r["found"])
        n_expected = n_found + len(r["missed"])
        total_found += n_found
        total_expected += n_expected
        lines.append(f"{name}: {n_found}/{n_expected} ({r['recall']:.0%})")
        for text in r["missed"]:
            lines.append(f"    MISSED  {text}")
        for text in r["extra"]:
            lines.append(f"    extra   {text}")
    overall = total_found / total_expected if total_expected else 0.0
    lines.append(f"OVERALL stage-1 recall: {total_found}/{total_expected} ({overall:.0%})")
    return "\n".join(lines)
