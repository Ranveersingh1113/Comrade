"""Deterministic tests for the eval scoring logic (no LLM)."""
from evaluation.scoring import _is_subsequence, score


def test_subsequence():
    assert _is_subsequence(["a", "b"], ["x", "a", "y", "b"])
    assert not _is_subsequence(["b", "a"], ["a", "b"])  # order matters
    assert _is_subsequence([], ["a"])


def test_pass_with_expected_subsequence():
    called = [{"name": "team_get_state", "args": {}},
              {"name": "team_propose_task", "args": {"assignee_id": "u2"}}]
    r = score(called, ["team_propose_task"],
              arg_checks={"team_propose_task": {"assignee_id": "u2"}})
    assert r["passed"] and r["failures"] == []


def test_fail_when_expected_tool_missing():
    r = score([{"name": "team_get_state", "args": {}}], ["team_propose_task"])
    assert not r["passed"]


def test_fail_on_forbidden_tool():
    called = [{"name": "team_propose_task", "args": {}}]
    r = score(called, ["team_propose_task"], forbidden_tools=("member_send_nudge",))
    assert r["passed"]  # no forbidden called
    r2 = score([{"name": "member_send_nudge", "args": {}}],
               ["member_send_nudge"], forbidden_tools=("member_send_nudge",))
    assert not r2["passed"]


def test_fail_on_wrong_arg():
    called = [{"name": "team_propose_task", "args": {"assignee_id": "WRONG"}}]
    r = score(called, ["team_propose_task"],
              arg_checks={"team_propose_task": {"assignee_id": "u2"}})
    assert not r["passed"]
