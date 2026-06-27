"""Live smoke: run one agent turn through the runtime and print the recorded run.

Seeds Team A, runs a turn, prints the reply + the persisted agent_runs row, then
cleans up. Makes a real Gemini call.

Run: uv run python scripts/smoke_runtime.py
"""
import psycopg

from agent.runtime import run_turn_sync
from shared.agent_runs import get_run
from shared.config import settings
from tests._seed import A1, TEAM_A, cleanup, seed


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def main() -> None:
    conn = _admin()
    try:
        with conn.cursor() as cur:
            cleanup(cur)
            seed(cur)
    finally:
        conn.close()
    try:
        result = run_turn_sync(TEAM_A, A1, "Give me a short status summary.")
        print(f"[REPLY] {result['reply']}")
        run = get_run(TEAM_A, result["run_id"])
        print(f"[RUN] status={run['status']} steps={run['current_step']}")
        for step in run["steps"]:
            print(f"  - {step['type']}: {step.get('tool') or step.get('text', '')}")
    finally:
        conn = _admin()
        try:
            with conn.cursor() as cur:
                cleanup(cur)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
