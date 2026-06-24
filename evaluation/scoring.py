"""Deterministic scoring for tool-routing eval.

Pure functions: given the tool calls an agent actually made and what a scenario
expected, decide pass/fail. No LLM here (so this is unit-tested in the gate);
producing the `called` list requires a live agent run (evaluation/runner.py).
"""


def _is_subsequence(sub: list[str], seq: list[str]) -> bool:
    """True if `sub` appears in `seq` in order (not necessarily contiguous)."""
    it = iter(seq)
    return all(item in it for item in sub)


def score(
    called: list[dict],
    expected_tools: list[str],
    forbidden_tools: tuple[str, ...] = (),
    arg_checks: dict[str, dict] | None = None,
) -> dict:
    """Score one scenario.

    called: [{"name": str, "args": dict}, ...] in call order.
    expected_tools: must appear, in this order (subsequence).
    forbidden_tools: must NOT appear.
    arg_checks: {tool_name: {arg_key: expected_value}} on the first such call.
    """
    names = [c["name"] for c in called]
    failures: list[str] = []

    if not _is_subsequence(list(expected_tools), names):
        failures.append(f"expected {list(expected_tools)} as subsequence of {names}")

    hit_forbidden = [f for f in forbidden_tools if f in names]
    if hit_forbidden:
        failures.append(f"forbidden tool(s) called: {hit_forbidden}")

    for tool, checks in (arg_checks or {}).items():
        call = next((c for c in called if c["name"] == tool), None)
        if call is None:
            failures.append(f"{tool} not called (needed for arg check)")
            continue
        for key, expected in checks.items():
            actual = call.get("args", {}).get(key)
            if actual != expected:
                failures.append(f"{tool}.{key} = {actual!r}, expected {expected!r}")

    return {"passed": not failures, "called": names, "failures": failures}
