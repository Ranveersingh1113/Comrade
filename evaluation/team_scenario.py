"""Deterministic scoring for the four-person team scenario (sim/scenario.py).

Pure functions: given what actually happened — DB rows, consent-queue rows,
active memory facts, and the agent's own ordered tool-call steps — decide
pass/fail. No LLM here, no DB, no network, so this is unit-tested in the
default gate. `score_team_scenario` is `evaluation.scoring.score`'s sibling
for the multi-turn team scenario rather than single-prompt tool routing.

PROSE IS NEVER AUTHORITY. A live run once had the agent reply "I have
proposed three tasks for your approval" while zero consent_queue rows
existed, and the script still exited 0. Nothing here reads a reply, a
transcript line, or any other free text to decide whether something
happened — only rows (consent_queue, memory_versions, memory_citations) and
agent_steps, which is where the tool actually got called or didn't.

FORBIDDEN-FACT DETECTION IS PROVENANCE, NOT PHRASING. An earlier version of
this file flagged a fact by matching obligation phrases ("must be", "need
to") in its text. Checked against a real run (sim/evidence.json), that
marker list produced one false positive — it failed a genuine team decision,
"The game must be dependency-free (standard library only)," because of its
wording — and zero true positives, because neither real example from the
plan was phrased with a marker that run. Citations fix both: every fact
carries citations back to the message(s) it was extracted from, and that
message is either something said to the ROOM (sim.scenario.say(), plain
chat) or something addressed TO COMRADE (sim.scenario.ask(), an agent-turn
input). A fact whose citations are ALL agent-turn inputs is restating what
someone asked Comrade to do, not recording a decision — regardless of how
it's phrased.
"""
import re

#: Dropped when normalizing a fact for the duplicate check below — common
#: words that make two paraphrases of the same decision look different
#: without changing what the decision is.
_STOPWORDS = frozenset({
    "a", "an", "and", "at", "in", "is", "of", "on", "the", "to", "with",
})


def _is_request_fact(citations: list[str], agent_input_message_ids: set[str]) -> bool:
    """True if every citation for this fact points at a message that was
    itself the input to an agent turn (TurnResponse.user_message_id), rather
    than plain chat. A fact entirely sourced from what someone asked Comrade
    to do restates that request; it is not a team decision no matter how
    it's worded.

    A fact with NO citations returns False — absent provenance is not
    evidence of a request, and failing on a gap this scorer cannot explain
    would be worse than not checking at all. A fact cited to a MIX of room
    chat and agent-turn input also returns False: at least one citation
    grounds it in something the team actually said, independent of Comrade.
    """
    if not citations:
        return False
    return all(source_id in agent_input_message_ids for source_id in citations)


def _normalize_fact(text: str) -> frozenset[str]:
    """Lowercase, drop punctuation, drop stopwords, return the remaining
    words as a set. Two facts are "the same decision" for the duplicate
    check below when one's word set is a subset of the other's — the
    observed duplicate ("The snake starts with a length of 3" vs "The snake
    in the game starts with length 3 in the middle, moving right") is
    exactly a subset relationship once stopwords are removed, and a
    containment rule is enough to catch it without a similarity score nobody
    can defend a threshold for.
    """
    words = re.findall(r"[a-z0-9]+", text.lower())
    return frozenset(w for w in words if w not in _STOPWORDS)


def _duplicate_facts(facts: list[str]) -> tuple[str, str] | None:
    """The first pair of active facts that normalize to the same thing, or
    None. Only the first pair is reported — one duplicate is enough to fail
    the run and point a human at the memory page."""
    normalized = [(f, _normalize_fact(f)) for f in facts if f]
    for i in range(len(normalized)):
        text_a, words_a = normalized[i]
        if not words_a:
            continue
        for text_b, words_b in normalized[i + 1:]:
            if words_b and (words_a <= words_b or words_b <= words_a):
                return text_a, text_b
    return None


def _verified_before_proposal(steps: list[dict]) -> bool:
    """True if a repo_run that actually completed sits between the last
    repo_edit step and the (first) repo_propose_pr step, in `steps` order.

    A repo_run that itself errored (sandbox unavailable, refused command —
    see agent/repo_tools.py's `{"error": ...}` shape) does not count: nothing
    was actually verified. A non-zero exit code DOES count — repo_run's own
    contract is that a failing test run is a normal, informative answer, not
    an error; this check is only asking "did Comrade look", not "did it
    pass".

    When there is no repo_edit or no repo_propose_pr step at all, this check
    has nothing to judge and returns True — the missing-PR case is already
    covered by the pr_consents count in score_team_scenario.
    """
    edit_idxs = [i for i, s in enumerate(steps) if s.get("tool") == "repo_edit"]
    propose_idxs = [i for i, s in enumerate(steps)
                     if s.get("tool") == "repo_propose_pr"]
    if not edit_idxs or not propose_idxs:
        return True
    last_edit, propose = edit_idxs[-1], propose_idxs[0]
    for step in steps[last_edit + 1:propose]:
        response = step.get("response")
        if (step.get("type") == "tool_result" and step.get("tool") == "repo_run"
                and isinstance(response, dict) and "error" not in response):
            return True
    return False


def score_team_scenario(evidence: dict) -> dict:
    """Score one run of the four-person scenario.

    evidence (all keys read with .get(..., default) so a partial dict scores
    instead of raising):
      runs:          [{"input_tokens": int, "output_tokens": int,
                       "seconds": float, "status": str}, ...] one per
                      agent_runs row. Metrics count every run, including
                      failed ones — a turn that failed spent the team's
                      tokens exactly like one that succeeded.
      http_errors:   [...] non-200 responses from ask() (POST /agent/turn).
      repo_cloned:   bool — github_repos.last_cloned_at is not null.
      task_consents: [consent_queue row, ...] where tool_name == "task_create".
      pr_consents:   [consent_queue row, ...] where tool_name == "repo_open_pr".
      memory_facts:  [{"fact": str, "citations": [source_id: str, ...]}, ...]
                      one per active (is_active) memory_versions row; citations
                      are memory_citations.source_id for that version (any
                      source_kind — message, document, github).
      agent_input_message_ids: [str, ...] — messages.id of every message that
                      was a POST /agent/turn input (TurnResponse.user_message_id),
                      i.e. something addressed TO Comrade rather than plain chat.
      steps:         [{"type": "tool_call"|"tool_result"|"text",
                       "tool": str | None, "response": dict | None}, ...]
                      every agent_steps row for the scenario, in chronological
                      order.

    Returns {"passed": bool, "failures": [str, ...], "metrics": {...}}.
    """
    failures: list[str] = []
    metrics = {
        "input_tokens": sum(r.get("input_tokens", 0) for r in evidence.get("runs", [])),
        "output_tokens": sum(r.get("output_tokens", 0) for r in evidence.get("runs", [])),
        "latency_seconds": sum(r.get("seconds", 0) for r in evidence.get("runs", [])),
    }

    if evidence.get("http_errors"):
        failures.append("agent HTTP request failed")

    # 🔴 Two of six live turns ended 'failed' and this scorer said the run
    # passed. Both had run tools and then produced no text, so the API
    # answered 200 with an empty reply and the member got silence. Ruling 4
    # says never grade on prose — status is not prose, it is the column the
    # runtime writes when it gives up, and reading it costs nothing.
    #
    # Only an explicitly non-done status fails: absent means this evidence
    # never recorded one, which is a different claim from "the turn failed".
    failed = [r.get("status") for r in evidence.get("runs", [])
              if r.get("status") is not None and r.get("status") != "done"]
    if failed:
        failures.append(
            f"{len(failed)} agent run(s) did not end 'done': {failed}"
        )
    if not evidence.get("repo_cloned"):
        failures.append("repository did not clone")
    if len(evidence.get("task_consents", [])) != 3:
        failures.append("expected exactly three task actions")
    if len(evidence.get("pr_consents", [])) != 1:
        failures.append("expected exactly one PR action")

    facts = evidence.get("memory_facts", [])
    agent_input_ids = set(evidence.get("agent_input_message_ids", []))

    forbidden = [f.get("fact", "") for f in facts
                 if _is_request_fact(f.get("citations", []), agent_input_ids)]
    if forbidden:
        failures.append(f"forbidden memory request-fact(s): {forbidden}")

    dupe = _duplicate_facts([f.get("fact", "") for f in facts])
    if dupe is not None:
        failures.append(f"duplicate normalized facts: {dupe[0]!r} / {dupe[1]!r}")

    if not _verified_before_proposal(evidence.get("steps", [])):
        failures.append(
            "no successful repo_run step between the last repo_edit and the "
            "repo_propose_pr step (absent verification before proposal)"
        )

    return {"passed": not failures, "failures": failures, "metrics": metrics}
