"""Deterministic tests for the team-scenario scorer (no LLM, no DB, no network).

sim/scenario.py scripts four people building a snake game against the real
API, real RLS and a real GitHub repo. It used to be a smoke script that
printed and continued no matter what happened underneath it — a live run once
reported "I have proposed three tasks for your approval" while zero consent
rows existed, and still exited 0. `score_team_scenario` is what makes that
impossible: it reads DB rows and agent_steps, never the assistant's reply
text, and fails the run when they disagree with what actually happened.

Each test below is a static evidence dict standing in for what
sim/scenario.py's evidence collector would have read from the database. One
"healthy" baseline evidence dict passes every check; each failing test
mutates exactly one thing away from health so the failure it produces is
unambiguous.
"""
from evaluation.team_scenario import score_team_scenario


def _healthy_evidence() -> dict:
    return {
        "runs": [{"input_tokens": 120, "output_tokens": 340, "seconds": 4.5}],
        "http_errors": [],
        "repo_cloned": True,
        "task_consents": [{"id": "t1"}, {"id": "t2"}, {"id": "t3"}],
        "pr_consents": [{"id": "p1"}],
        # Citations point at plain room messages (say()), not at an
        # agent-turn input — neither is a request-fact.
        "memory_facts": [
            {"fact": "The grid is 20x20.", "citations": ["msg-room-1"]},
            {"fact": "Marcus takes the game loop and collision.",
             "citations": ["msg-room-2"]},
        ],
        "agent_input_message_ids": ["msg-ask-unrelated"],
        "steps": [
            {"type": "tool_call", "tool": "repo_edit", "response": None},
            {"type": "tool_result", "tool": "repo_edit", "response": {"ok": True}},
            {"type": "tool_call", "tool": "repo_run", "response": None},
            {"type": "tool_result", "tool": "repo_run",
             "response": {"exit_code": 0, "stdout": "", "stderr": ""}},
            {"type": "tool_call", "tool": "repo_propose_pr", "response": None},
            {"type": "tool_result", "tool": "repo_propose_pr", "response": {"ok": True}},
        ],
    }


def test_healthy_evidence_passes():
    r = score_team_scenario(_healthy_evidence())
    assert r["passed"], r["failures"]
    assert r["failures"] == []
    assert r["metrics"] == {
        "input_tokens": 120, "output_tokens": 340, "latency_seconds": 4.5,
    }


def test_partial_evidence_dict_scores_without_raising():
    # ruling 1: evidence.get("runs", []) everywhere, not evidence["runs"] —
    # a partial/empty evidence dict must score, not raise.
    r = score_team_scenario({})
    assert r["passed"] is False
    assert r["metrics"] == {
        "input_tokens": 0, "output_tokens": 0, "latency_seconds": 0,
    }


def test_fails_on_missing_http_result():
    evidence = _healthy_evidence()
    evidence["http_errors"] = [
        {"who": "Tom Alvarez", "text": "...", "detail": "500 internal error"}
    ]
    r = score_team_scenario(evidence)
    assert not r["passed"]
    assert "agent HTTP request failed" in r["failures"]


def test_fails_on_missing_repo_clone():
    evidence = _healthy_evidence()
    evidence["repo_cloned"] = False
    r = score_team_scenario(evidence)
    assert not r["passed"]
    assert "repository did not clone" in r["failures"]


def test_fails_when_reply_claims_three_tasks_but_zero_rows_exist():
    # This is the exact bug that motivated this file: the agent's reply said
    # "I have proposed three tasks for your approval" while consent_queue had
    # zero task_create rows for the team. The scorer must not be fooled by a
    # "reply" sitting anywhere in evidence — only task_consents counts.
    evidence = _healthy_evidence()
    evidence["task_consents"] = []
    evidence["runs"][0]["reply"] = "I have proposed three tasks for your approval."
    r = score_team_scenario(evidence)
    assert not r["passed"]
    assert "expected exactly three task actions" in r["failures"]


def test_fails_on_absent_pr_proposal():
    evidence = _healthy_evidence()
    evidence["pr_consents"] = []
    r = score_team_scenario(evidence)
    assert not r["passed"]
    assert "expected exactly one PR action" in r["failures"]


def test_fails_on_forbidden_memory_request_fact():
    # Provenance decides, not phrasing — this fact carries no obligation
    # language at all ("are assigned", not "must be assigned"). It is still
    # a request-fact because every citation is an agent-turn input.
    evidence = _healthy_evidence()
    evidence["agent_input_message_ids"] = ["msg-ask-tasks"]
    evidence["memory_facts"] = [
        {"fact": "The grid is 20x20.", "citations": ["msg-room-1"]},
        {"fact": "The created tasks are assigned to the right people.",
         "citations": ["msg-ask-tasks"]},
    ]
    r = score_team_scenario(evidence)
    assert not r["passed"]
    assert any("forbidden memory request-fact" in f for f in r["failures"])


def test_request_fact_provenance_regression():
    # The exact real case from the live run (sim/evidence.json) that
    # replaced the phrasing-based marker list with this rule: the marker
    # list failed a genuine decision because of its wording and missed a
    # real request-fact because it wasn't marked. Provenance gets both
    # right regardless of phrasing.
    evidence = _healthy_evidence()
    evidence["agent_input_message_ids"] = ["msg-ask-create-tasks"]
    evidence["memory_facts"] = [
        {
            # Priya's kickoff say() — a genuine team decision, phrased with
            # "must be". Must NOT be flagged.
            "fact": "The game must be dependency-free (standard library only).",
            "citations": ["msg-room-kickoff"],
        },
        {
            # Priya's ask() asking Comrade to create the tasks — a
            # request-fact. Must be flagged, even though "need to" was the
            # only marker the old heuristic ever caught by luck.
            "fact": "Tasks need to be created for the three agreed work items",
            "citations": ["msg-ask-create-tasks"],
        },
    ]
    r = score_team_scenario(evidence)
    assert not r["passed"]
    forbidden_line = next(f for f in r["failures"]
                           if "forbidden memory request-fact" in f)
    assert "Tasks need to be created" in forbidden_line
    assert "dependency-free" not in forbidden_line


def test_fact_with_no_citations_is_not_a_request_fact():
    evidence = _healthy_evidence()
    evidence["memory_facts"] = [
        {"fact": "The grid is 20x20.", "citations": []},
    ]
    r = score_team_scenario(evidence)
    assert r["passed"], r["failures"]


def test_fact_cited_to_both_room_and_agent_input_is_not_flagged():
    # At least one citation grounds the fact in something the team said
    # independent of Comrade, so it is not "restating a request".
    evidence = _healthy_evidence()
    evidence["agent_input_message_ids"] = ["msg-ask-1"]
    evidence["memory_facts"] = [
        {"fact": "The grid is 20x20.", "citations": ["msg-room-1", "msg-ask-1"]},
    ]
    r = score_team_scenario(evidence)
    assert r["passed"], r["failures"]


def test_fails_on_duplicate_normalized_facts():
    # The real observed duplicate: same decision, two different phrasings.
    evidence = _healthy_evidence()
    evidence["memory_facts"] = [
        {"fact": "The snake starts with a length of 3", "citations": ["msg-room-1"]},
        {"fact": "The snake in the game starts with length 3 in the middle, "
                 "moving right.", "citations": ["msg-room-1"]},
    ]
    r = score_team_scenario(evidence)
    assert not r["passed"]
    assert any("duplicate normalized facts" in f for f in r["failures"])


def test_distinct_facts_are_not_flagged_as_duplicates():
    evidence = _healthy_evidence()
    evidence["memory_facts"] = [
        {"fact": "The grid is 20x20.", "citations": ["msg-room-1"]},
        {"fact": "Tom writes the tests.", "citations": ["msg-room-1"]},
        {"fact": "No curses; render a plain text grid to stdout.",
         "citations": ["msg-room-1"]},
    ]
    r = score_team_scenario(evidence)
    assert r["passed"], r["failures"]


def test_fails_on_absent_verification_before_proposal():
    # repo_edit happens, then repo_propose_pr happens, with no repo_run in
    # between — Comrade proposed a PR without ever running the tests it
    # claims to have run. Task 4 later enforces this in the product; Task 1
    # only has to detect it here.
    evidence = _healthy_evidence()
    evidence["steps"] = [
        {"type": "tool_call", "tool": "repo_edit", "response": None},
        {"type": "tool_result", "tool": "repo_edit", "response": {"ok": True}},
        {"type": "tool_call", "tool": "repo_propose_pr", "response": None},
        {"type": "tool_result", "tool": "repo_propose_pr", "response": {"ok": True}},
    ]
    r = score_team_scenario(evidence)
    assert not r["passed"]
    assert any("verif" in f for f in r["failures"])


def test_errored_repo_run_between_edit_and_proposal_does_not_count_as_verification():
    evidence = _healthy_evidence()
    evidence["steps"] = [
        {"type": "tool_call", "tool": "repo_edit", "response": None},
        {"type": "tool_call", "tool": "repo_run", "response": None},
        {"type": "tool_result", "tool": "repo_run",
         "response": {"error": "sandbox unavailable"}},
        {"type": "tool_call", "tool": "repo_propose_pr", "response": None},
    ]
    r = score_team_scenario(evidence)
    assert not r["passed"]
    assert any("verif" in f for f in r["failures"])


def test_evidence_writer_refuses_to_write_a_live_token():
    # ruling 6: state.json holds live JWTs and WHO carries them in memory for
    # the run; the evidence artifact must never end up with one in it. Cheap
    # insurance, tested without touching the DB or the network. Checked
    # against an actual WHO token value, not the word "token" — a repo_run
    # or GitHub tool result can legitimately contain that word (e.g. a
    # "invalid syntax" traceback, or a token_type field) without leaking
    # anything, and a keyword match on those would be a false alarm.
    from sim.scenario import WHO, _write_evidence

    live_token = next(iter(WHO.values()))["token"]
    try:
        _write_evidence({"leaked": live_token}, path=None)
    except ValueError:
        pass
    else:
        raise AssertionError("expected _write_evidence to refuse a live access token")


def test_evidence_writer_writes_clean_evidence(tmp_path):
    from sim.scenario import _write_evidence

    out = _write_evidence({"repo_cloned": True, "task_consents": []},
                           path=tmp_path / "evidence.json")
    assert out.exists()
    assert "token" not in out.read_text(encoding="utf-8").lower()


def test_fails_when_a_run_ended_failed():
    """🔴 The gate scored a run GREEN while a third of its turns failed.

    The live scenario had two of six turns come back HTTP 200 with an empty
    reply — the model ran tools and then said nothing. `agent_runs.status`
    recorded both as failed, with zero text steps between them, and one of
    the two had already written three pending consent rows.

    Nothing here read a reply to notice that, and nothing needed to: status
    is a column. Ruling 4 forbids grading on prose, not on rows, and this is
    the check that closes the gap it deliberately left open.
    """
    evidence = _healthy_evidence()
    evidence["runs"] = [
        {"input_tokens": 100, "output_tokens": 50, "seconds": 2.0, "status": "done"},
        {"input_tokens": 200, "output_tokens": 0, "seconds": 3.0, "status": "failed"},
    ]
    r = score_team_scenario(evidence)
    assert r["passed"] is False
    assert any("failed" in f for f in r["failures"]), r["failures"]
    # Metrics still count every run, failed ones included: a turn that failed
    # spent the team's tokens exactly like one that succeeded.
    assert r["metrics"]["input_tokens"] == 300


def test_a_run_with_no_status_recorded_does_not_fail_the_check():
    """Partial evidence scores rather than raising (ruling 1), and an absent
    status is not the same claim as a failed one."""
    evidence = _healthy_evidence()
    evidence["runs"] = [{"input_tokens": 10, "output_tokens": 5, "seconds": 1.0}]
    r = score_team_scenario(evidence)
    assert r["passed"], r["failures"]


def test_repo_run_before_the_last_edit_does_not_count_as_verification():
    # Verification has to come AFTER the last edit, not just anywhere in the
    # transcript — a test run against yesterday's code proves nothing about
    # today's.
    evidence = _healthy_evidence()
    evidence["steps"] = [
        {"type": "tool_call", "tool": "repo_run", "response": None},
        {"type": "tool_result", "tool": "repo_run",
         "response": {"exit_code": 0, "stdout": "", "stderr": ""}},
        {"type": "tool_call", "tool": "repo_edit", "response": None},
        {"type": "tool_result", "tool": "repo_edit", "response": {"ok": True}},
        {"type": "tool_call", "tool": "repo_propose_pr", "response": None},
    ]
    r = score_team_scenario(evidence)
    assert not r["passed"]
    assert any("verif" in f for f in r["failures"])
