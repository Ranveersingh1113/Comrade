# Production Agent Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Comrade a real, HTTP-callable agent entrypoint that runs one agent turn and records every step to `agent_runs` for observability — replacing the test-only `InMemoryRunner` invocation path.

**Architecture:** A thin FastAPI service exposes `POST /agent/turn`. The handler binds `team_id`/`requester_id` server-side and calls an orchestrator (`agent/runtime.py`) that runs the existing ADK `root_agent` for a single turn, streaming its events through a pure event→step mapper and persisting each step to the `agent_runs` table under the `comrade_agent` RLS role. Conversation history stays in the `messages` table (source of truth), so each turn uses a fresh, stateless ADK runner — no durable ADK session is needed for this slice.

**Tech Stack:** Python 3.12, Google ADK (`google-adk>=2.2.0`), psycopg 3, Pydantic, FastAPI + Uvicorn (new), pytest. Postgres via the existing role-scoped connection URLs in `.env`.

## Global Constraints

- Python `>=3.12`; dependencies managed with `uv` (`uv add`, `uv run`). Never call `pip` directly.
- All database access goes through `shared/db.py` role helpers — `team_session(Role.AGENT, team_id)` for this runtime. **Never** use `service_role` or the admin URL for agent work.
- `team_id` and `requester_id` are **server-bound**: injected into ADK session `state`, never passed to the model as tool arguments. The HTTP handler is the binding point.
- Tests use `pytest`, live in `tests/`, run via `uv run pytest` (config: `pythonpath = ["."]`, `testpaths = ["tests"]`). DB-touching tests require a local Postgres with the migrations in `supabase/migrations/` and the roles from `scripts/setup_local_roles.sql` applied — the existing suite already assumes this.
- Type-annotate every function signature; follow PEP 8.
- Conventional commits (`feat:`, `fix:`, `chore:`, `docs:`, `test:`). Commit after each task.
- No new external services. Only Postgres (existing) and Gemini (existing key) are used.

### Out of scope for this plan (deferred to follow-up plans)

Event bus / cron / webhook / document-action triggers; auth (sourcing `team_id`/`requester_id` from a validated Supabase JWT instead of the request body); durable multi-turn history loading; persisting the AI reply back to the `messages` table; the `SET LOCAL` + transaction-pooler hardening and retiring the superuser `user_session`. This plan delivers the synchronous request/response turn + observability spine that those build on.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `shared/agent_runs.py` (create) | Durable run/step logging: open a run, append ordered steps, close it — all under the `comrade_agent` role, team-scoped. |
| `agent/runtime.py` (create) | Pure event→step mapping helpers + the turn orchestrator that wires ADK to `agent_runs`. |
| `server/__init__.py` (create) | Package marker. |
| `server/app.py` (create) | FastAPI app: `GET /health`, `POST /agent/turn`. The server-binding point for `team_id`/`requester_id`. |
| `scripts/smoke_runtime.py` (create) | Live manual smoke: seed a team, run one turn through the runtime, print the recorded run. |
| `tests/test_agent_runs.py` (create) | Unit/integration tests for the run/step logger (DB, no LLM). |
| `tests/test_runtime_mapping.py` (create) | Unit tests for the pure event→step mapper (no DB, no LLM). |
| `tests/test_runtime_live.py` (create) | Live integration test for the orchestrator (skipped without a Gemini key). |
| `tests/test_server.py` (create) | HTTP tests with the orchestrator stubbed (no DB, no LLM). |
| `README.md` (modify) | Add the runtime to the repo layout + a "Run the agent service" section. |

---

## Task 1: Run/step observability module (`shared/agent_runs.py`)

**Files:**
- Create: `shared/agent_runs.py`
- Test: `tests/test_agent_runs.py`

**Interfaces:**
- Consumes: `shared.db.Role`, `shared.db.team_session` (existing); the `seeded` fixture from `tests/conftest.py`; `TEAM_A`, `TEAM_B` from `tests/_seed.py`.
- Produces:
  - `start_run(team_id: str, trigger_type: str, input_summary: str) -> str` (returns `run_id`)
  - `append_step(team_id: str, run_id: str, step: dict[str, Any]) -> None`
  - `finish_run(team_id: str, run_id: str, status: str) -> None`
  - `get_run(team_id: str, run_id: str) -> dict[str, Any] | None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_agent_runs.py`:

```python
"""agent_runs observability: durable run + step logging under the AGENT role."""
from shared.agent_runs import append_step, finish_run, get_run, start_run
from tests._seed import TEAM_A, TEAM_B


def test_start_run_creates_running_row(seeded):
    run_id = start_run(TEAM_A, "user", "give me a status summary")
    run = get_run(TEAM_A, run_id)
    assert run is not None
    assert run["status"] == "running"
    assert run["current_step"] == 0
    assert run["steps"] == []
    assert run["trigger_type"] == "user"


def test_append_step_orders_and_counts(seeded):
    run_id = start_run(TEAM_A, "user", "summary")
    append_step(TEAM_A, run_id, {"seq": 0, "type": "tool_call", "tool": "team_get_state"})
    append_step(TEAM_A, run_id, {"seq": 1, "type": "text", "text": "All caught up."})
    run = get_run(TEAM_A, run_id)
    assert run["current_step"] == 2
    assert [s["type"] for s in run["steps"]] == ["tool_call", "text"]


def test_finish_run_sets_terminal_status(seeded):
    run_id = start_run(TEAM_A, "user", "summary")
    finish_run(TEAM_A, run_id, "done")
    run = get_run(TEAM_A, run_id)
    assert run["status"] == "done"
    assert run["finished_at"] is not None


def test_runs_are_team_scoped(seeded):
    run_id = start_run(TEAM_A, "user", "summary")
    # TEAM_B's agent session must not see TEAM_A's run (RLS via current_team()).
    assert get_run(TEAM_B, run_id) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_agent_runs.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shared.agent_runs'`.

- [ ] **Step 3: Write the implementation**

Create `shared/agent_runs.py`:

```python
"""Durable run/step logging for agent turns (observability + crash recovery).

Each turn opens an agent_runs row, appends one step per tool call / tool result /
text chunk as it happens, then closes the row done|failed. All writes go through
the AGENT role + team_session, so RLS (team_id = current_team()) confines them to
the one team.
"""
from typing import Any

from psycopg.types.json import Json

from shared.db import Role, team_session


def start_run(team_id: str, trigger_type: str, input_summary: str) -> str:
    """Open a running agent_runs row. Returns the run id."""
    with team_session(Role.AGENT, team_id) as conn:
        row = conn.execute(
            "insert into public.agent_runs (team_id, trigger_type, input_summary)"
            " values (%s, %s, %s) returning id",
            (team_id, trigger_type, input_summary),
        ).fetchone()
    return str(row[0])


def append_step(team_id: str, run_id: str, step: dict[str, Any]) -> None:
    """Append one ordered step to the run and bump current_step."""
    with team_session(Role.AGENT, team_id) as conn:
        conn.execute(
            "update public.agent_runs"
            " set steps = steps || %s::jsonb, current_step = current_step + 1"
            " where id = %s",
            (Json([step]), run_id),
        )


def finish_run(team_id: str, run_id: str, status: str) -> None:
    """Close the run with a terminal status ('done' | 'failed')."""
    with team_session(Role.AGENT, team_id) as conn:
        conn.execute(
            "update public.agent_runs set status = %s, finished_at = now()"
            " where id = %s",
            (status, run_id),
        )


def get_run(team_id: str, run_id: str) -> dict[str, Any] | None:
    """Read a run back under the AGENT role (team-scoped). None if not visible."""
    with team_session(Role.AGENT, team_id) as conn:
        row = conn.execute(
            "select id, trigger_type, input_summary, steps, current_step, status,"
            " finished_at from public.agent_runs where id = %s",
            (run_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": str(row[0]),
        "trigger_type": row[1],
        "input_summary": row[2],
        "steps": row[3],
        "current_step": row[4],
        "status": row[5],
        "finished_at": row[6].isoformat() if row[6] else None,
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_agent_runs.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add shared/agent_runs.py tests/test_agent_runs.py
git commit -m "feat: durable agent_runs step logging under the agent role"
```

---

## Task 2: Pure event→step mapping (`agent/runtime.py`)

**Files:**
- Create: `agent/runtime.py`
- Test: `tests/test_runtime_mapping.py`

**Interfaces:**
- Consumes: nothing (pure helpers; duck-types ADK event objects via `getattr`).
- Produces:
  - `_steps_from_event(event: Any, start_seq: int) -> list[dict[str, Any]]`
  - `_reply_from_steps(steps: list[dict[str, Any]]) -> str`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_runtime_mapping.py`:

```python
"""Pure event->step mapping for the agent runtime (no LLM, no DB)."""
from types import SimpleNamespace

from agent.runtime import _reply_from_steps, _steps_from_event


def _event(*parts):
    return SimpleNamespace(content=SimpleNamespace(parts=list(parts)))


def _call_part(name, args):
    return SimpleNamespace(
        function_call=SimpleNamespace(name=name, args=args),
        function_response=None, text=None,
    )


def _result_part(name, response):
    return SimpleNamespace(
        function_call=None,
        function_response=SimpleNamespace(name=name, response=response),
        text=None,
    )


def _text_part(text):
    return SimpleNamespace(function_call=None, function_response=None, text=text)


def test_maps_tool_call_part():
    steps = _steps_from_event(_event(_call_part("team_get_state", {})), 0)
    assert steps == [
        {"seq": 0, "type": "tool_call", "tool": "team_get_state", "args": {}}
    ]


def test_maps_result_and_text_with_continuing_seq():
    ev = _event(
        _result_part("team_get_state", {"members": []}),
        _text_part("All caught up."),
    )
    steps = _steps_from_event(ev, 3)
    assert [s["seq"] for s in steps] == [3, 4]
    assert steps[0]["type"] == "tool_result"
    assert steps[1] == {"seq": 4, "type": "text", "text": "All caught up."}


def test_empty_event_yields_nothing():
    assert _steps_from_event(SimpleNamespace(content=None), 0) == []


def test_reply_joins_text_steps_only():
    steps = [
        {"seq": 0, "type": "tool_call", "tool": "x", "args": {}},
        {"seq": 1, "type": "text", "text": "Hello "},
        {"seq": 2, "type": "text", "text": "world."},
    ]
    assert _reply_from_steps(steps) == "Hello world."
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_runtime_mapping.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.runtime'`.

- [ ] **Step 3: Write the implementation**

Create `agent/runtime.py`:

```python
"""Run a single agent turn and record it to agent_runs.

Conversation history is the messages table's job (source of truth); each turn
uses a fresh, stateless ADK runner seeded with the server-bound team/requester.
The orchestrator (run_turn / run_turn_sync) is added in Task 3.
"""
from typing import Any


def _steps_from_event(event: Any, start_seq: int) -> list[dict[str, Any]]:
    """Map one ADK event's parts to ordered step dicts.

    Pure: reads function_call / function_response / text off each part. seq
    continues from start_seq so steps stay globally ordered across events.
    """
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) or []
    steps: list[dict[str, Any]] = []
    seq = start_seq
    for part in parts:
        fc = getattr(part, "function_call", None)
        fr = getattr(part, "function_response", None)
        text = getattr(part, "text", None)
        if fc is not None:
            steps.append({
                "seq": seq, "type": "tool_call",
                "tool": fc.name, "args": dict(fc.args or {}),
            })
            seq += 1
        elif fr is not None:
            steps.append({
                "seq": seq, "type": "tool_result",
                "tool": fr.name, "response": fr.response,
            })
            seq += 1
        elif text:
            steps.append({"seq": seq, "type": "text", "text": text})
            seq += 1
    return steps


def _reply_from_steps(steps: list[dict[str, Any]]) -> str:
    """Concatenate the text steps into the user-facing reply."""
    return "".join(s["text"] for s in steps if s["type"] == "text").strip()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_runtime_mapping.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add agent/runtime.py tests/test_runtime_mapping.py
git commit -m "feat: pure ADK event-to-step mapping for the runtime"
```

---

## Task 3: Turn orchestrator (`agent/runtime.py`)

**Files:**
- Modify: `agent/runtime.py` (add imports + two functions below the Task 2 helpers)
- Test: `tests/test_runtime_live.py`

**Interfaces:**
- Consumes: `agent.agent.root_agent` (existing); `shared.agent_runs.start_run` / `append_step` / `finish_run` (Task 1); `_steps_from_event` / `_reply_from_steps` (Task 2); ADK `InMemoryRunner`, `google.genai.types`.
- Produces:
  - `async run_turn(team_id: str, requester_id: str, user_text: str, trigger_type: str = "user") -> dict[str, Any]`
  - `run_turn_sync(team_id: str, requester_id: str, user_text: str, trigger_type: str = "user") -> dict[str, Any]`
  - Both return `{"run_id": str, "reply": str, "steps": list[dict]}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_runtime_live.py`:

```python
"""Live integration: a real agent turn records steps and returns a reply.

Requires GEMINI_API_KEY and a reachable DB; skipped without a key. Makes a real
Gemini call.
"""
import pytest

from agent.runtime import run_turn_sync
from shared.agent_runs import get_run
from shared.config import settings
from tests._seed import A1, TEAM_A

pytestmark = pytest.mark.skipif(
    not settings.gemini_api_key, reason="no GEMINI_API_KEY configured"
)


def test_run_turn_records_and_replies(seeded):
    result = run_turn_sync(TEAM_A, A1, "Give me a short status summary.")
    assert result["reply"]
    run = get_run(TEAM_A, result["run_id"])
    assert run["status"] == "done"
    assert run["current_step"] == len(result["steps"])
    # The agent should consult team state before summarising.
    assert any(
        s["type"] == "tool_call" and s["tool"] == "team_get_state"
        for s in result["steps"]
    )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_runtime_live.py -v`
Expected: FAIL with `ImportError: cannot import name 'run_turn_sync' from 'agent.runtime'` (or SKIPPED if no Gemini key — in that case set `GEMINI_API_KEY` in `.env` to exercise it, otherwise the import error is the signal that the function is missing).

- [ ] **Step 3: Write the implementation**

Edit `agent/runtime.py`. Replace the top-of-file import line `from typing import Any` with this import block:

```python
import asyncio
from typing import Any

from google.adk.runners import InMemoryRunner
from google.genai import types

from agent.agent import root_agent
from shared.agent_runs import append_step, finish_run, start_run

_APP_NAME = "comrade"
```

Then append these two functions to the end of `agent/runtime.py` (after `_reply_from_steps`):

```python
async def run_turn(
    team_id: str, requester_id: str, user_text: str, trigger_type: str = "user"
) -> dict[str, Any]:
    """Run one agent turn; record every step to agent_runs. Returns the result.

    team_id / requester_id are SERVER-BOUND here and injected into ADK session
    state — the model receives them via state, never as tool arguments.
    """
    run_id = start_run(team_id, trigger_type, user_text[:200])
    runner = InMemoryRunner(agent=root_agent, app_name=_APP_NAME)
    session = await runner.session_service.create_session(
        app_name=_APP_NAME, user_id=requester_id,
        state={"team_id": team_id, "requester_id": requester_id},
    )
    message = types.Content(role="user", parts=[types.Part(text=user_text)])
    all_steps: list[dict[str, Any]] = []
    try:
        async for event in runner.run_async(
            user_id=requester_id, session_id=session.id, new_message=message
        ):
            for step in _steps_from_event(event, len(all_steps)):
                append_step(team_id, run_id, step)
                all_steps.append(step)
    except Exception:
        finish_run(team_id, run_id, "failed")
        raise
    finish_run(team_id, run_id, "done")
    return {
        "run_id": run_id,
        "reply": _reply_from_steps(all_steps),
        "steps": all_steps,
    }


def run_turn_sync(
    team_id: str, requester_id: str, user_text: str, trigger_type: str = "user"
) -> dict[str, Any]:
    """Blocking wrapper around run_turn for sync callers (the HTTP handler)."""
    return asyncio.run(run_turn(team_id, requester_id, user_text, trigger_type))
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_runtime_live.py -v`
Expected: PASS if `GEMINI_API_KEY` is set in `.env` (1 passed); SKIPPED otherwise. Also re-run the prior task to confirm no regression: `uv run pytest tests/test_runtime_mapping.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/runtime.py tests/test_runtime_live.py
git commit -m "feat: agent turn orchestrator wiring ADK to agent_runs"
```

---

## Task 4: HTTP service (`server/app.py`)

**Files:**
- Modify: `pyproject.toml` (add deps via `uv add`)
- Create: `server/__init__.py`, `server/app.py`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `agent.runtime.run_turn_sync` (Task 3).
- Produces: a FastAPI app object `server.app:app` with `GET /health` and `POST /agent/turn` (body `{team_id, requester_id, text}` → `{run_id, reply}`).

- [ ] **Step 1: Add the web dependencies**

Run:
```bash
uv add fastapi "uvicorn[standard]"
uv add --dev httpx
```
Expected: `pyproject.toml` gains `fastapi` and `uvicorn[standard]` under `dependencies`, and `httpx` under the `dev` group; `uv.lock` updates. (`httpx` is required by FastAPI's `TestClient`.)

- [ ] **Step 2: Write the failing tests**

Create `tests/test_server.py`:

```python
"""HTTP layer for the agent runtime (orchestrator stubbed — no LLM, no DB)."""
from fastapi.testclient import TestClient

from server.app import app

client = TestClient(app)


def test_health_ok():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_agent_turn_returns_reply(monkeypatch):
    def _stub(team_id, requester_id, user_text, trigger_type="user"):
        assert (team_id, requester_id, user_text) == ("team-1", "user-1", "status?")
        return {"run_id": "run-1", "reply": "All caught up.", "steps": []}

    # Patch where it is used (server.app imported the name).
    monkeypatch.setattr("server.app.run_turn_sync", _stub)
    resp = client.post(
        "/agent/turn",
        json={"team_id": "team-1", "requester_id": "user-1", "text": "status?"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"run_id": "run-1", "reply": "All caught up."}
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_server.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server'`.

- [ ] **Step 4: Write the implementation**

Create `server/__init__.py` (empty file):

```python
```

Create `server/app.py`:

```python
"""HTTP entrypoint for the Comrade agent.

POST /agent/turn runs one agent turn and returns its reply + run id. team_id and
requester_id are read into server-side variables here (the binding point); a
later auth task will source them from the validated Supabase JWT instead of the
request body. The model never receives them as tool arguments.
"""
from fastapi import FastAPI
from pydantic import BaseModel

from agent.runtime import run_turn_sync

app = FastAPI(title="Comrade Agent Runtime")


class TurnRequest(BaseModel):
    team_id: str
    requester_id: str
    text: str


class TurnResponse(BaseModel):
    run_id: str
    reply: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/agent/turn", response_model=TurnResponse)
def agent_turn(req: TurnRequest) -> TurnResponse:
    # Server-bound here; swap the source to JWT claims in the auth task.
    team_id = req.team_id
    requester_id = req.requester_id
    result = run_turn_sync(team_id, requester_id, req.text)
    return TurnResponse(run_id=result["run_id"], reply=result["reply"])
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_server.py -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock server/__init__.py server/app.py tests/test_server.py
git commit -m "feat: FastAPI agent runtime entrypoint (POST /agent/turn)"
```

---

## Task 5: Local smoke script + README

**Files:**
- Create: `scripts/smoke_runtime.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `agent.runtime.run_turn_sync` (Task 3); `shared.agent_runs.get_run` (Task 1); `tests._seed` helpers (existing); `shared.config.settings` (existing).
- Produces: a runnable smoke command and updated docs. (No automated test — this task's deliverable is manual verification + documentation.)

- [ ] **Step 1: Write the smoke script**

Create `scripts/smoke_runtime.py`:

```python
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
```

- [ ] **Step 2: Run the smoke script to verify the end-to-end path**

Run: `uv run python scripts/smoke_runtime.py`
Expected (with `GEMINI_API_KEY` set and the local DB up): a printed `[REPLY] ...` line, a `[RUN] status=done steps=N` line, and at least one `tool_call: team_get_state` step. If `GEMINI_API_KEY` is unset, expect an auth error from Gemini — set the key first.

- [ ] **Step 3: Update the README**

In `README.md`, add a row to the "Repository layout" table (after the `agent/` row):

```markdown
| `server/` | FastAPI runtime — `POST /agent/turn` runs one agent turn |
```

And add this section immediately after the "Setup" section:

```markdown
## Run the agent service

```bash
uv run uvicorn server.app:app --reload      # serves on http://127.0.0.1:8000

curl -s http://127.0.0.1:8000/agent/turn \
  -H 'content-type: application/json' \
  -d '{"team_id":"<team-uuid>","requester_id":"<user-uuid>","text":"Give me a status summary."}'
```

Every turn is recorded to `public.agent_runs` (one row per turn, one step per tool
call / result / text chunk) for observability. `team_id` / `requester_id` are
bound server-side; sourcing them from the Supabase JWT is the next task.
```

- [ ] **Step 4: Run the full suite to confirm no regressions**

Run: `uv run pytest -v`
Expected: all prior tests plus the new `test_agent_runs`, `test_runtime_mapping`, `test_server` pass; `test_runtime_live` passes or is skipped depending on the Gemini key.

- [ ] **Step 5: Commit**

```bash
git add scripts/smoke_runtime.py README.md
git commit -m "docs: agent runtime smoke script and run instructions"
```

---

## Self-Review

**1. Spec coverage** (against the chosen Phase-0 slice — "a durable, observable HTTP entrypoint for the agent"):
- HTTP entrypoint → Task 4 (`POST /agent/turn`, `GET /health`). ✓
- Real agent tool-loop run (not test-only) → Task 3 (`run_turn` via ADK `run_async`). ✓
- `agent_runs` observability (the unused table) → Task 1 (logger) + Task 3 (wiring). ✓
- Stateless-per-request runner (history stays in `messages`) → Task 3 architecture note. ✓
- Manual verification path → Task 5 smoke + README. ✓
- Explicitly deferred (event bus, auth/JWT binding, pooler hardening, reply persistence, durable history) → listed under Global Constraints "Out of scope". ✓ No gap left silent.

**2. Placeholder scan:** No "TBD/TODO/handle edge cases/similar to Task N". Every code step contains complete, runnable code. The one forward-reference ("swap the source to JWT in the auth task") is an explicit scope note, not a placeholder in code to fill.

**3. Type consistency:**
- `start_run/append_step/finish_run/get_run` signatures defined in Task 1 are used identically in Task 3 (`start_run(team_id, trigger_type, user_text[:200])`, `append_step(team_id, run_id, step)`, `finish_run(team_id, run_id, "done"|"failed")`) and Task 5 (`get_run(team_id, run_id)`). ✓
- `_steps_from_event(event, start_seq)` / `_reply_from_steps(steps)` defined in Task 2 used identically in Task 3 (`_steps_from_event(event, len(all_steps))`, `_reply_from_steps(all_steps)`). ✓
- `run_turn_sync(team_id, requester_id, user_text, trigger_type="user") -> {"run_id","reply","steps"}` defined in Task 3, consumed in Task 4 (`result["run_id"]`, `result["reply"]`) and Task 5 (`result["reply"]`, `result["run_id"]`). ✓
- Step dict shape `{"seq","type","tool"/"args"/"response"/"text"}` is consistent between Task 2 (producer), Task 1 tests (consumer), and Task 5 (`step.get('tool')`/`step.get('text')`). ✓

No issues found.
