"""Tool-routing evaluation (live LLM). Seeds a team, runs each scenario through
the agent, scores tool routing, prints a report. Exit 1 if any scenario fails.

Run: uv run python -m evaluation.run
"""
import sys

import psycopg

from evaluation.runner import run_agent
from evaluation.scenarios import SCENARIOS
from evaluation.scoring import score
from shared.config import settings
from tests._seed import cleanup, seed


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def main() -> int:
    conn = _admin()
    with conn.cursor() as cur:
        cleanup(cur)
        seed(cur)
    conn.close()

    results = []
    try:
        for sc in SCENARIOS:
            called = run_agent(sc["prompt"], sc["state"])
            r = score(
                called,
                sc["expected"],
                forbidden_tools=sc.get("forbidden", ()),
                arg_checks=sc.get("arg_checks"),
            )
            results.append((sc["name"], r))
    finally:
        conn = _admin()
        with conn.cursor() as cur:
            cleanup(cur)
        conn.close()

    passed = sum(1 for _, r in results if r["passed"])
    print(f"\n=== Tool-routing eval: {passed}/{len(results)} passed ===")
    for name, r in results:
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"[{mark}] {name}  called={r['called']}")
        for f in r["failures"]:
            print(f"        - {f}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
