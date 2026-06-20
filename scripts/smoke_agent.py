"""Live smoke test: seed a team, ask Comrade for its status, observe the tool
call + final reply, then clean up. Makes a real Gemini call.

Run: uv run python scripts/smoke_agent.py
"""
import asyncio

import psycopg
from google.genai import types

from agent.agent import root_agent
from shared.config import settings
from tests._seed import A1, TEAM_A, cleanup, seed


def _seed_team():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cleanup(cur)
            seed(cur)
            # one live task so the summary has something to report
            cur.execute(
                "insert into public.tasks (team_id, assignee_id, title, status,"
                " created_by_kind, created_by_id) select %s, user_id, 'Draft the report',"
                " 'proposed', 'user', user_id from public.memberships"
                " where team_id=%s and role='member' limit 1",
                (TEAM_A, TEAM_A),
            )
    finally:
        conn.close()


def _cleanup_team():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cleanup(cur)
    finally:
        conn.close()


async def main():
    _seed_team()
    try:
        from google.adk.runners import InMemoryRunner

        runner = InMemoryRunner(agent=root_agent, app_name="comrade")
        user_id = "smoke-user"
        # team_id / requester_id are server-bound in session state (never LLM args)
        session = await runner.session_service.create_session(
            app_name="comrade", user_id=user_id,
            state={"team_id": TEAM_A, "requester_id": A1},
        )
        prompt = "Give me a short status summary."
        msg = types.Content(role="user", parts=[types.Part(text=prompt)])

        async for ev in runner.run_async(
            user_id=user_id, session_id=session.id, new_message=msg
        ):
            if not (ev.content and ev.content.parts):
                continue
            for p in ev.content.parts:
                if getattr(p, "function_call", None):
                    print(f"[TOOL CALL] {p.function_call.name} args={dict(p.function_call.args)}")
                if getattr(p, "function_response", None):
                    print(f"[TOOL RESULT] {p.function_response.name} -> {p.function_response.response}")
                if p.text:
                    print(f"[AGENT] {p.text}")
    finally:
        _cleanup_team()


if __name__ == "__main__":
    asyncio.run(main())
