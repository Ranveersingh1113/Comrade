# Agent Memory + Streaming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the agent read the team wiki (Claude Code's auto-loaded-memory + load-on-demand model), and stream the turn instead of blocking for it.

**Architecture:** The instruction becomes a callable that appends the wiki's page index each turn (ADK accepts `Callable[[ReadonlyContext], str]`). A `memory_read_page` tool pulls one page's facts + citations on demand. `run_turn` is re-expressed as a consumer of a new `stream_turn` async generator, and a second endpoint streams that generator as newline-delimited JSON.

**Tech Stack:** Google ADK (`LlmAgent`, `ToolContext`, `ReadonlyContext`), FastAPI `StreamingResponse`, psycopg, pytest, React + `fetch`/`ReadableStream`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-21-agent-memory-and-streaming-design.md`.
- No embeddings, no vector store, no similarity search — the model picks pages by title/description.
- `team_id` and `requester_id` are **server-bound from session state**, never model arguments.
- The agent **never writes memory**. `Role.AGENT` has `select` only on `memory_*`; do not add grants.
- `POST /agent/turn` keeps its exact current contract — the existing suite must pass untouched.
- Membership and turn-budget checks run **before** streaming starts, so 403/429 stay real HTTP statuses.
- Conventional commits, **no attribution trailers**.
- Backend tests: `uv run pytest -q` (live tier excluded by default). Live: `uv run pytest -m live -q`.
- Frontend: `npm test` (unit+component), `npm run build` must typecheck clean.

---

### Task 1: Auto-load the wiki page index into the instruction

**Files:**
- Modify: `agent/agent.py`
- Test: `tests/test_agent_memory.py` (create)

**Interfaces:**
- Consumes: `pipeline.wiki.all_active_pages(conn, team_id) -> list[dict]` with keys `page_id`, `title`, `description`, `facts` (each fact `{"entry_id", "text"}`); `shared.db.team_session(Role.AGENT, team_id)`.
- Produces: `agent.agent.wiki_section(team_id: str) -> str` and `agent.agent.build_instruction(ctx) -> str`. `root_agent.instruction` becomes the callable.

- [ ] **Step 1: Write the failing test**

Create `tests/test_agent_memory.py`:

```python
"""The agent's view of the team wiki (Claude Code's auto-loaded memory)."""
import psycopg

from agent.agent import wiki_section
from shared.config import settings
from tests._seed import TEAM_A, TEAM_B


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _seed_page(cur, team_id, title, fact, description=""):
    page_id = cur.execute(
        "insert into public.memory_pages (team_id, title, description)"
        " values (%s,%s,%s) returning id",
        (team_id, title, description),
    ).fetchone()[0]
    entry_id = cur.execute(
        "insert into public.memory_entries (team_id, page_id) values (%s,%s)"
        " returning id",
        (team_id, page_id),
    ).fetchone()[0]
    version_id = cur.execute(
        "insert into public.memory_versions (entry_id, team_id, fact, change_type)"
        " values (%s,%s,%s,'added') returning id",
        (entry_id, team_id, fact),
    ).fetchone()[0]
    return page_id, entry_id, version_id


def test_index_lists_page_titles_and_descriptions(seeded):
    conn = _admin()
    try:
        with conn.cursor() as cur:
            _seed_page(cur, TEAM_A, "Deadlines", "Demo is Friday",
                       description="key dates")
    finally:
        conn.close()

    section = wiki_section(TEAM_A)
    assert "Deadlines" in section
    assert "key dates" in section
    # the index is titles only — never the facts themselves
    assert "Demo is Friday" not in section


def test_index_omits_pages_with_no_active_facts(seeded):
    conn = _admin()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "insert into public.memory_pages (team_id, title) values (%s,'Hollow')",
                (TEAM_A,),
            )
    finally:
        conn.close()
    assert "Hollow" not in wiki_section(TEAM_A)


def test_empty_wiki_says_so(seeded):
    section = wiki_section(TEAM_A)
    assert "empty" in section.lower()


def test_index_is_team_scoped(seeded):
    conn = _admin()
    try:
        with conn.cursor() as cur:
            _seed_page(cur, TEAM_B, "OtherTeamSecrets", "not yours")
    finally:
        conn.close()
    assert "OtherTeamSecrets" not in wiki_section(TEAM_A)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_memory.py -q`
Expected: FAIL — `ImportError: cannot import name 'wiki_section'`.

- [ ] **Step 3: Implement**

In `agent/agent.py`, add imports at the top (after the existing imports):

```python
from google.adk.agents.readonly_context import ReadonlyContext

from pipeline.wiki import all_active_pages
from shared.db import Role, team_session
```

Add above `root_agent`:

```python
def wiki_section(team_id: str) -> str:
    """The team wiki's page index — titles and descriptions only.

    Claude Code's model: the index is always in context, page bodies load on
    demand (memory_read_page). Pages with no active facts are omitted so the
    agent never opens an empty one.
    """
    with team_session(Role.AGENT, team_id) as conn:
        pages = [p for p in all_active_pages(conn, team_id) if p["facts"]]
    if not pages:
        return (
            "\n## The team wiki\n"
            "The wiki is empty — nothing has been compiled yet. Say so plainly"
            " rather than guessing at decisions or deadlines.\n"
        )
    lines = "\n".join(
        f"- {p['title']}" + (f" — {p['description']}" if p["description"] else "")
        for p in pages
    )
    return (
        "\n## The team wiki\n"
        "Compiled from the team's own documents and chat. Every fact is cited,"
        " versioned, and revertible by any member.\n\n"
        f"{lines}\n\n"
        "Call memory_read_page with a title before answering about decisions,"
        " deadlines, scope, or history. Cite what you find. If the wiki does not"
        " say it, say that it does not.\n"
    )


def build_instruction(ctx: ReadonlyContext) -> str:
    """Per-turn instruction: static rules + this team's wiki index."""
    return INSTRUCTION + wiki_section(ctx.state["team_id"])
```

Change the agent construction from `instruction=INSTRUCTION` to:

```python
    instruction=build_instruction,
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_agent_memory.py -q`
Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add agent/agent.py tests/test_agent_memory.py
git commit -m "feat: auto-load the team wiki index into the agent instruction"
```

---

### Task 2: `memory_read_page` tool

**Files:**
- Modify: `agent/tools.py`, `agent/agent.py` (register the tool)
- Test: `tests/test_agent_memory.py` (append)

**Interfaces:**
- Consumes: `pipeline.wiki.all_active_pages`; `ToolContext.state["team_id"]`.
- Produces: `agent.tools.read_memory_page(team_id: str, title: str) -> dict` (pure, explicit team) and `agent.tools.memory_read_page(title: str, tool_context: ToolContext) -> dict` (the ADK tool). Success shape: `{"title", "description", "facts": [{"fact", "citations": [{"source_kind", "source_id", "excerpt"}]}]}`. Failure shape: `{"error": "no such page", "available": [str]}`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_agent_memory.py`:

```python
def test_read_page_returns_facts_with_citations(seeded):
    from agent.tools import read_memory_page

    conn = _admin()
    try:
        with conn.cursor() as cur:
            _, _, version_id = _seed_page(
                cur, TEAM_A, "Deadlines", "Demo is Friday", description="key dates",
            )
            doc_id = cur.execute(
                "insert into public.documents (team_id, kind, filename)"
                " values (%s,'text','plan.txt') returning id",
                (TEAM_A,),
            ).fetchone()[0]
            cur.execute(
                "insert into public.memory_citations (version_id, source_kind,"
                " source_id, excerpt) values (%s,'document',%s,'demo on Friday')",
                (version_id, doc_id),
            )
    finally:
        conn.close()

    page = read_memory_page(TEAM_A, "Deadlines")
    assert page["title"] == "Deadlines"
    assert page["facts"][0]["fact"] == "Demo is Friday"
    citation = page["facts"][0]["citations"][0]
    assert citation["source_kind"] == "document"
    assert citation["excerpt"] == "demo on Friday"


def test_read_page_is_case_insensitive(seeded):
    from agent.tools import read_memory_page

    conn = _admin()
    try:
        with conn.cursor() as cur:
            _seed_page(cur, TEAM_A, "Deadlines", "Demo is Friday")
    finally:
        conn.close()
    assert read_memory_page(TEAM_A, "deadlines")["title"] == "Deadlines"


def test_unknown_page_lists_what_is_available(seeded):
    from agent.tools import read_memory_page

    conn = _admin()
    try:
        with conn.cursor() as cur:
            _seed_page(cur, TEAM_A, "Deadlines", "Demo is Friday")
    finally:
        conn.close()

    out = read_memory_page(TEAM_A, "Budget")
    assert out["error"] == "no such page"
    assert out["available"] == ["Deadlines"]


def test_read_page_cannot_reach_another_team(seeded):
    from agent.tools import read_memory_page

    conn = _admin()
    try:
        with conn.cursor() as cur:
            _seed_page(cur, TEAM_B, "OtherTeamSecrets", "not yours")
    finally:
        conn.close()

    out = read_memory_page(TEAM_A, "OtherTeamSecrets")
    assert out["error"] == "no such page"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_memory.py -q`
Expected: FAIL — `ImportError: cannot import name 'read_memory_page'`.

- [ ] **Step 3: Implement**

In `agent/tools.py`, add to the imports at the top:

```python
from pipeline.wiki import all_active_pages
```

Add after `fetch_team_state` (keeping the pure-function-then-tool pattern the file already uses):

```python
def read_memory_page(team_id: str, title: str) -> dict:
    """One wiki page's active facts with their citations (pure; explicit team).

    Titles match case-insensitively — the model reads them off an index, so a
    capitalisation slip should not read as "no such page".
    """
    with team_session(Role.AGENT, team_id) as conn:
        pages = [p for p in all_active_pages(conn, team_id) if p["facts"]]
        page = next(
            (p for p in pages if p["title"].lower() == title.strip().lower()), None
        )
        if page is None:
            return {
                "error": "no such page",
                "available": [p["title"] for p in pages],
            }
        entry_ids = [f["entry_id"] for f in page["facts"]]
        rows = conn.execute(
            "select v.entry_id, c.source_kind, c.source_id, c.excerpt"
            " from public.memory_versions v"
            " join public.memory_citations c on c.version_id = v.id"
            " where v.entry_id = any(%s::uuid[]) and v.is_active",
            (entry_ids,),
        ).fetchall()

    by_entry: dict[str, list[dict]] = {}
    for entry_id, source_kind, source_id, excerpt in rows:
        by_entry.setdefault(str(entry_id), []).append(
            {
                "source_kind": source_kind,
                "source_id": str(source_id),
                "excerpt": excerpt,
            }
        )
    return {
        "title": page["title"],
        "description": page["description"],
        "facts": [
            {"fact": f["text"], "citations": by_entry.get(f["entry_id"], [])}
            for f in page["facts"]
        ],
    }
```

Add the ADK tool alongside the other tools (after `team_get_state`):

```python
def memory_read_page(title: str, tool_context: ToolContext) -> dict:
    """Read one page of the team wiki: its facts and where each came from.

    Use this before answering about decisions, deadlines, scope, or history.
    The page titles are listed in your instructions. Cite what you find. If a
    page does not contain the answer, say the wiki does not record it.

    Args:
        title: a page title from the wiki index in your instructions.
    """
    return read_memory_page(tool_context.state["team_id"], title)
```

In `agent/agent.py`, add `memory_read_page` to the tools import and to the `tools=[...]` list, immediately after `team_get_state`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_agent_memory.py -q`
Expected: `8 passed`.

- [ ] **Step 5: Commit**

```bash
git add agent/tools.py agent/agent.py tests/test_agent_memory.py
git commit -m "feat: memory_read_page tool — agent reads wiki pages on demand"
```

---

### Task 3: `stream_turn` generator + `/agent/turn/stream`

**Files:**
- Modify: `agent/runtime.py`
- Modify: `server/app.py`
- Test: `tests/test_server_stream.py` (create)

**Interfaces:**
- Consumes: `agent.runtime._steps_from_event`, `_reply_from_steps`; `shared.agent_runs.start_run/append_step/finish_run`; `server.app._persist_user_message`, `_persist_ai_reply`, `require_membership`, `_check_turn_budget`.
- Produces: `agent.runtime.stream_turn(team_id, requester_id, user_text, trigger_type="user")` — async generator yielding `{"type": "run", "run_id": str}`, then step dicts (each already carrying `type` of `tool_call` / `tool_result` / `text`), then `{"type": "final", "run_id": str, "reply": str}`. `run_turn` keeps its exact current return shape `{"run_id", "reply", "steps"}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_server_stream.py`:

```python
"""Streaming turn: NDJSON frames, and the guards that run before the stream."""
import json

import pytest
from fastapi.testclient import TestClient

from server.app import app
from server.auth import current_user_id
from shared.config import settings
from tests._seed import A1, B1, TEAM_A


async def _fake_stream(team_id, requester_id, user_text, trigger_type="user"):
    yield {"type": "run", "run_id": "run-1"}
    yield {"seq": 0, "type": "tool_call", "tool": "team_get_state", "args": {}}
    yield {"seq": 1, "type": "text", "text": "The demo is Friday."}
    yield {"type": "final", "run_id": "run-1", "reply": "The demo is Friday."}


@pytest.fixture
def as_a1(monkeypatch):
    monkeypatch.setattr("server.app.stream_turn", _fake_stream)
    monkeypatch.setattr("server.app._persist_user_message", lambda *a: "msg-user")
    monkeypatch.setattr("server.app._persist_ai_reply", lambda *a: "msg-ai")
    app.dependency_overrides[current_user_id] = lambda: A1
    yield TestClient(app)
    app.dependency_overrides.clear()


def _frames(resp) -> list[dict]:
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def test_stream_emits_run_steps_then_final(seeded, as_a1):
    resp = as_a1.post(
        "/agent/turn/stream", json={"team_id": TEAM_A, "text": "when is the demo?"}
    )
    assert resp.status_code == 200
    frames = _frames(resp)
    assert frames[0] == {"type": "run", "run_id": "run-1"}
    assert any(f["type"] == "tool_call" and f["tool"] == "team_get_state" for f in frames)
    assert any(f["type"] == "text" for f in frames)
    done = frames[-1]
    assert done["type"] == "done"
    assert done["user_message_id"] == "msg-user"
    assert done["reply_message_id"] == "msg-ai"


def test_non_member_gets_403_not_a_stream(seeded, monkeypatch):
    """The guard must fail as a real status, never as a 200 whose body says no."""
    monkeypatch.setattr("server.app.stream_turn", _fake_stream)
    app.dependency_overrides[current_user_id] = lambda: B1
    try:
        resp = TestClient(app).post(
            "/agent/turn/stream", json={"team_id": TEAM_A, "text": "hi"}
        )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_over_budget_gets_429_not_a_stream(seeded, as_a1, monkeypatch):
    monkeypatch.setattr(settings, "agent_turns_per_hour", 1)
    import psycopg

    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    try:
        conn.execute(
            "insert into public.agent_runs (team_id, trigger_type, status)"
            " values (%s,'user','done')",
            (TEAM_A,),
        )
    finally:
        conn.close()
    resp = as_a1.post("/agent/turn/stream", json={"team_id": TEAM_A, "text": "hi"})
    assert resp.status_code == 429


def test_empty_reply_persists_no_ai_message(seeded, as_a1, monkeypatch):
    async def _silent(team_id, requester_id, user_text, trigger_type="user"):
        yield {"type": "run", "run_id": "run-2"}
        yield {"type": "final", "run_id": "run-2", "reply": ""}

    monkeypatch.setattr("server.app.stream_turn", _silent)
    resp = as_a1.post("/agent/turn/stream", json={"team_id": TEAM_A, "text": "hi"})
    assert _frames(resp)[-1]["reply_message_id"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_server_stream.py -q`
Expected: FAIL — `AttributeError: <module 'server.app'> does not have the attribute 'stream_turn'`.

- [ ] **Step 3: Re-express `run_turn` over a generator**

In `agent/runtime.py`, replace the whole `run_turn` function with:

```python
async def stream_turn(
    team_id: str, requester_id: str, user_text: str, trigger_type: str = "user"
) -> AsyncIterator[dict[str, Any]]:
    """Run one turn, yielding each step as it happens and recording it.

    team_id / requester_id are SERVER-BOUND here and injected into ADK session
    state — the model receives them via state, never as tool arguments.

    Yields {"type": "run", ...} first, then one dict per step, then
    {"type": "final", ...}. run_turn() below is a consumer of this, so the
    orchestration exists once.
    """
    run_id = await run_in_threadpool(start_run, team_id, trigger_type, user_text[:200])
    yield {"type": "run", "run_id": run_id}

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
                await run_in_threadpool(append_step, team_id, run_id, step)
                all_steps.append(step)
                yield step
    except Exception:
        await run_in_threadpool(finish_run, team_id, run_id, "failed")
        raise
    await run_in_threadpool(finish_run, team_id, run_id, "done")
    yield {
        "type": "final",
        "run_id": run_id,
        "reply": _reply_from_steps(all_steps),
    }


async def run_turn(
    team_id: str, requester_id: str, user_text: str, trigger_type: str = "user"
) -> dict[str, Any]:
    """Batch form of stream_turn: drain it and return the collected result."""
    steps: list[dict[str, Any]] = []
    final: dict[str, Any] = {}
    async for item in stream_turn(team_id, requester_id, user_text, trigger_type):
        if item.get("type") == "run":
            continue
        if item.get("type") == "final":
            final = item
            continue
        steps.append(item)
    return {"run_id": final["run_id"], "reply": final["reply"], "steps": steps}
```

Add to the imports at the top of `agent/runtime.py`:

```python
from typing import Any, AsyncIterator

from starlette.concurrency import run_in_threadpool
```

(replacing the existing `from typing import Any`).

- [ ] **Step 4: Verify the batch path is unchanged**

Run: `uv run pytest tests/test_runtime_mapping.py tests/test_server.py -q`
Expected: all pass — `run_turn`'s contract is identical.

- [ ] **Step 5: Add the streaming endpoint**

In `server/app.py`, add to the imports:

```python
import json

from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from agent.runtime import run_turn_sync, stream_turn
```

(the `agent.runtime` import replaces the existing `from agent.runtime import run_turn_sync`.)

Add after the `agent_turn` handler:

```python
@app.post("/agent/turn/stream")
async def agent_turn_stream(req: TurnRequest, user_id: CurrentUserId):
    """Same turn as /agent/turn, delivered as newline-delimited JSON.

    NDJSON over fetch rather than SSE: EventSource cannot send an
    Authorization header, and a token in the query string would leak into
    logs. One JSON object per line, no frame parsing.

    Both guards run BEFORE the response starts, so a non-member still gets a
    real 403 and an over-budget team a real 429 — never a 200 whose first
    frame is an apology.
    """
    require_membership(user_id, req.team_id)
    _check_turn_budget(user_id, req.team_id)
    owner = None if req.thread_type == "group" else user_id
    user_message_id = await run_in_threadpool(
        _persist_user_message, user_id, req.team_id, req.thread_type, req.text
    )

    async def frames():
        reply = ""
        try:
            async for item in stream_turn(req.team_id, user_id, req.text):
                if item.get("type") == "final":
                    reply = item["reply"]
                    continue
                yield json.dumps(item) + "\n"
        except Exception as exc:  # noqa: BLE001 - the stream owns its errors
            logger.exception("streamed turn failed")
            yield json.dumps({"type": "error", "detail": str(exc)}) + "\n"
            return
        reply_message_id = None
        if reply:
            reply_message_id = await run_in_threadpool(
                _persist_ai_reply, req.team_id, req.thread_type, owner, reply
            )
        yield json.dumps({
            "type": "done",
            "user_message_id": user_message_id,
            "reply_message_id": reply_message_id,
        }) + "\n"

    return StreamingResponse(frames(), media_type="application/x-ndjson")
```

Add near the top of `server/app.py`, after the imports:

```python
logger = logging.getLogger(__name__)
```

and add `import logging` to the imports.

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_server_stream.py -q`
Expected: `4 passed`.

- [ ] **Step 7: Full backend suite**

Run: `uv run pytest -q`
Expected: all pass, 0 failures.

- [ ] **Step 8: Commit**

```bash
git add agent/runtime.py server/app.py tests/test_server_stream.py
git commit -m "feat: stream agent turns as NDJSON; run_turn now drains the stream"
```

---

### Task 4: Frontend consumes the stream

**Files:**
- Modify: `frontend/src/lib/agentApi.ts`
- Modify: `frontend/src/screens/PrivateThread.tsx`
- Modify: `frontend/src/screens/GroupRoom.tsx`
- Test: `frontend/tests/component/PrivateThread.test.tsx` (append)

**Interfaces:**
- Consumes: `POST /agent/turn/stream` NDJSON frames — `{"type":"run"|"tool_call"|"tool_result"|"text"|"done"|"error", ...}`.
- Produces: `agentApi.streamTurn(teamId: string, text: string, threadType: 'private' | 'group', onFrame: (f: StreamFrame) => void): Promise<void>` and the exported `StreamFrame` type.

- [ ] **Step 1: Write the failing test**

Append to `frontend/tests/component/PrivateThread.test.tsx`:

```typescript
test('streams the reply into the pending bubble as it arrives', async () => {
  const ndjson = [
    JSON.stringify({ type: 'run', run_id: 'r1' }),
    JSON.stringify({ type: 'tool_call', tool: 'team_get_state' }),
    JSON.stringify({ type: 'text', text: 'The demo ' }),
    JSON.stringify({ type: 'text', text: 'is Friday.' }),
    JSON.stringify({ type: 'done', user_message_id: 'm1', reply_message_id: 'm2' }),
  ].join('\n');
  server.use(
    http.post(`${BASE}/agent/turn/stream`, () =>
      new HttpResponse(ndjson, {
        headers: { 'Content-Type': 'application/x-ndjson' },
      }),
    ),
  );
  const user = userEvent.setup();
  renderInApp(<PrivateThread />);
  await user.type(screen.getByPlaceholderText(/this stays private/), 'when is the demo?');
  await user.click(screen.getByRole('button', { name: 'SEND' }));

  expect(await screen.findByText(/The demo is Friday\./)).toBeInTheDocument();
  expect(supaState.inserts).toHaveLength(0); // server persists, not the client
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run tests/component/PrivateThread.test.tsx`
Expected: FAIL — the text never appears (the screen still calls the batch endpoint).

- [ ] **Step 3: Add the streaming client**

In `frontend/src/lib/agentApi.ts`, append:

```typescript
export interface StreamFrame {
  type: 'run' | 'tool_call' | 'tool_result' | 'text' | 'done' | 'error';
  run_id?: string;
  tool?: string;
  text?: string;
  detail?: string;
  user_message_id?: string;
  reply_message_id?: string | null;
}

/**
 * Stream one agent turn, calling `onFrame` per NDJSON line.
 * fetch (not EventSource) because the turn needs the Authorization header.
 */
export async function streamTurn(
  teamId: string,
  text: string,
  threadType: 'private' | 'group',
  onFrame: (frame: StreamFrame) => void,
): Promise<void> {
  const res = await fetch(`${BASE}/agent/turn/stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: await authHeader(),
    },
    body: JSON.stringify({ team_id: teamId, text, thread_type: threadType }),
  });
  if (!res.ok || !res.body) {
    let detail = res.statusText;
    try {
      const j = (await res.json()) as { detail?: string };
      if (j.detail) detail = j.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new AgentApiError(res.status, detail);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() ?? '';           // keep the partial line
    for (const line of lines) {
      if (line.trim()) onFrame(JSON.parse(line) as StreamFrame);
    }
  }
  if (buffer.trim()) onFrame(JSON.parse(buffer) as StreamFrame);
}
```

- [ ] **Step 4: Use it in the private thread**

In `frontend/src/screens/PrivateThread.tsx`, change the import from
`import { agentTurn, agentErrorText } from '../lib/agentApi';` to
`import { streamTurn, agentErrorText } from '../lib/agentApi';`.

Add state next to the existing `waiting` state:

```tsx
  const [pending, setPending] = useState('');
  const [step, setStep] = useState('');
```

Replace the body of `send` with:

```tsx
  const send = async () => {
    const text = draft.trim();
    if (!text || !team) return;
    setDraft('');
    setSendError(null);
    setWaiting(true);
    setPending('');
    setStep('');
    try {
      await streamTurn(team.id, text, 'private', (f) => {
        if (f.type === 'text') setPending((p) => p + (f.text ?? ''));
        else if (f.type === 'tool_call') setStep(`checking ${f.tool}…`);
        else if (f.type === 'error') setSendError(f.detail ?? 'Turn failed');
      });
    } catch (e) {
      setSendError(agentErrorText(e));
      setDraft(text);
    } finally {
      setWaiting(false);
      setPending('');
      setStep('');
      await refresh();
    }
  };
```

Replace the `{waiting && (...)}` block with one that shows the streamed text
when there is any, and the blinking dots plus the current step when there is
not:

```tsx
          {waiting && (
            <div style={{ display: 'flex', gap: 13 }}>
              <AiOrb size={32} breathing />
              {pending ? (
                <div
                  style={{
                    background: 'var(--card)',
                    border: '1px solid var(--border)',
                    borderRadius: 3,
                    padding: '13px 16px',
                    fontSize: 13.5,
                    lineHeight: 1.55,
                    color: 'var(--text-body)',
                    maxWidth: 470,
                    whiteSpace: 'pre-wrap',
                  }}
                >
                  {pending}
                </div>
              ) : (
                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                  <span style={{ display: 'flex', gap: 4 }}>
                    {[0, 0.2, 0.4].map((d) => (
                      <span
                        key={d}
                        style={{
                          width: 6,
                          height: 6,
                          borderRadius: '50%',
                          background: 'var(--muted)',
                          animation: `blink 1.2s ${d}s infinite`,
                        }}
                      />
                    ))}
                  </span>
                  {step && (
                    <span className="mono" style={{ fontSize: 10, color: 'var(--faint)' }}>
                      {step}
                    </span>
                  )}
                </div>
              )}
            </div>
          )}
```

- [ ] **Step 5: Use it in the group room**

In `frontend/src/screens/GroupRoom.tsx`, change the import from
`import { agentTurn, agentErrorText, suppressObservation } from '../lib/agentApi';` to
`import { streamTurn, agentErrorText, suppressObservation } from '../lib/agentApi';`.

Add beside the existing `aiTyping` state:

```tsx
  const [pending, setPending] = useState('');
```

In `send`, replace `await agentTurn(teamId, text, 'group');` with:

```tsx
        setPending('');
        await streamTurn(teamId, text, 'group', (f) => {
          if (f.type === 'text') setPending((p) => p + (f.text ?? ''));
          else if (f.type === 'error') setSendError(f.detail ?? 'Turn failed');
        });
```

and add `setPending('');` beside the existing `setAiTyping(false);` in the
`finally` block.

In the `{aiTyping && (...)}` block, replace the three blinking dots with the
streamed text once it exists:

```tsx
                {pending ? (
                  <div style={{ paddingTop: 10, fontSize: 13.5, lineHeight: 1.5, whiteSpace: 'pre-wrap' }}>
                    {pending}
                  </div>
                ) : (
                  <div style={{ display: 'flex', gap: 4, alignItems: 'center', paddingTop: 13 }}>
                    {[0, 0.2, 0.4].map((d) => (
                      <span
                        key={d}
                        style={{
                          width: 6,
                          height: 6,
                          borderRadius: '50%',
                          background: 'var(--muted)',
                          animation: `blink 1.2s ${d}s infinite`,
                        }}
                      />
                    ))}
                  </div>
                )}
```

- [ ] **Step 6: Run frontend tests and build**

Run: `cd frontend && npm test && npm run build`
Expected: all tests pass; build typechecks clean.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/lib/agentApi.ts frontend/src/screens/PrivateThread.tsx frontend/src/screens/GroupRoom.tsx frontend/tests/component/PrivateThread.test.tsx
git commit -m "feat: stream agent replies into the room and private thread"
```

---

### Task 5: Live proof + docs

**Files:**
- Test: `tests/test_agent_memory_live.py` (create)
- Modify: `HANDOFF.md`

- [ ] **Step 1: Write the live test**

Create `tests/test_agent_memory_live.py`:

```python
"""Live proof that the agent actually reads the wiki (real Gemini)."""
import psycopg
import pytest

from agent.runtime import run_turn
from shared.config import settings
from tests._seed import TEAM_A, A1

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not settings.gemini_api_key, reason="no GEMINI_API_KEY configured"
    ),
]


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


@pytest.mark.asyncio
async def test_agent_answers_from_a_wiki_fact(seeded):
    conn = _admin()
    try:
        page_id = conn.execute(
            "insert into public.memory_pages (team_id, title, description)"
            " values (%s,'Deadlines','key dates') returning id",
            (TEAM_A,),
        ).fetchone()[0]
        entry_id = conn.execute(
            "insert into public.memory_entries (team_id, page_id) values (%s,%s)"
            " returning id",
            (TEAM_A, page_id),
        ).fetchone()[0]
        conn.execute(
            "insert into public.memory_versions (entry_id, team_id, fact, change_type)"
            " values (%s,%s,'The final demo is on 18 December','added')",
            (entry_id, TEAM_A),
        )
    finally:
        conn.close()

    result = await run_turn(TEAM_A, A1, "When is the final demo?")
    assert "18" in result["reply"] and "december" in result["reply"].lower()
    # it got there by reading the page, not by guessing
    assert any(
        s["type"] == "tool_call" and s["tool"] == "memory_read_page"
        for s in result["steps"]
    )
```

- [ ] **Step 2: Add the async test dependency**

`pytest.mark.asyncio` needs the plugin. Run:

```bash
uv add --dev pytest-asyncio
```

Then add to `pyproject.toml` under `[tool.pytest.ini_options]`:

```toml
asyncio_mode = "auto"
```

- [ ] **Step 3: Run the live test**

Run: `uv run pytest -m live -q`
Expected: `5 passed` (the 4 existing live tests plus this one).

- [ ] **Step 4: Update HANDOFF**

In §3, add a row to the endpoint table under `POST /agent/turn`:

```markdown
| `POST /agent/turn/stream` — same body | ✅ built | Newline-delimited JSON: `run` → `tool_call`/`text` → `done`. Use `fetch` + `ReadableStream`, not `EventSource` (it cannot send `Authorization`). 403/429 arrive as real statuses before the stream opens. |
```

In §6, add:

```markdown
9. **The agent reads the wiki but never writes it** — the page index is
   auto-loaded into its instruction each turn and `memory_read_page` pulls a
   page on demand. The compiler remains the sole writer.
```

- [ ] **Step 5: Commit**

```bash
git add tests/test_agent_memory_live.py pyproject.toml uv.lock HANDOFF.md
git commit -m "test: live proof the agent answers from the wiki; document the stream"
```

---

## Self-review

- **Spec coverage:** §A auto-loaded index → Task 1. §B `memory_read_page` → Task 2. §C streaming (generator, endpoint, guards-before-stream, NDJSON rationale) → Task 3; frontend scope → Task 4. Spec testing items 1–5 → Tasks 1–4; item 6 (live wiki answer) → Task 5.
- **Placeholders:** none — every code step carries its code.
- **Type consistency:** `wiki_section(team_id) -> str` and `build_instruction(ctx) -> str` (Task 1) are used nowhere later. `read_memory_page(team_id, title) -> dict` is the pure form; `memory_read_page(title, tool_context)` is the tool — Task 5's live test asserts the *tool* name `memory_read_page`, which matches what ADK registers. `stream_turn` yields `{"type": "final", ...}` internally and the endpoint converts it to the client-facing `{"type": "done", ...}` — Task 3's tests assert `done` at the client and `final` never reaches the wire.
- **Known risk flagged for the executor:** `run_turn_sync` calls `asyncio.run`, and `stream_turn` now uses `run_in_threadpool`, which requires a running event loop — `asyncio.run` provides one, so this is fine. If any caller invokes `run_turn_sync` from inside an existing loop it will still raise, exactly as it did before.
