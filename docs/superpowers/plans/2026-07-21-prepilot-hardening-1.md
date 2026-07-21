# Pre-pilot Hardening Slice 1 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close two invariant violations reachable with one curl (missing storage RLS, unguarded diff-card suppression) and the unmetered LLM spend hole on `/agent/turn`.

**Architecture:** Three independent fixes, each the shortest thing that actually closes its hole. Storage gets a bucket + two policies reusing the existing `public.is_team_member()` helper. Diff-card protection is one extra `not exists` clause in a query that already exists. The rate limit is a `count(*)` over `agent_runs`, which already logs every turn — no new table, no new dependency, correct across API instances.

**Tech Stack:** Postgres/Supabase migrations, FastAPI, psycopg, pytest, Vitest (integration layer via supabase-js).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-21-prepilot-hardening-1-design.md`.
- Never modify existing migrations — always add a new one.
- Conventional commits (`feat:`/`fix:`/`docs:`), **no attribution trailers**.
- Backend tests: `uv run pytest -q` (live tier excluded by default via `addopts`).
- The `agent_runs` timestamp column is **`created_at`** (not `started_at`).
- `public.is_team_member(_team_id uuid)` is `security definer` and already granted to `authenticated`.
- Existing index `idx_agent_runs_team on public.agent_runs(team_id, created_at)` covers the rate-limit query — do not add an index.
- Frontend needs **no changes** in this slice: uploads already write `{team_id}/{uuid}-{filename}` and reads already use `createSignedUrl`.

---

### Task 1: Storage bucket + storage RLS

**Files:**
- Create: `supabase/migrations/20260721090000_storage_documents.sql`
- Test: `frontend/tests/integration/storage.test.ts`

**Interfaces:**
- Consumes: `public.is_team_member(uuid)` (existing); harness `createUser`, `createTeam`, `cleanupTeam`, `stackUp` from `frontend/tests/integration/harness.ts`.
- Produces: bucket id `documents`; policies `st_documents_select`, `st_documents_insert`.

- [ ] **Step 1: Write the failing test**

Create `frontend/tests/integration/storage.test.ts`:

```typescript
/** Storage RLS: the documents bucket is tenant-scoped by its first path segment. */
import { afterAll, beforeAll, describe, expect, test } from 'vitest';
import {
  cleanupTeam, createTeam, createUser, stackUp, type TestUser,
} from './harness';

describe.skipIf(!stackUp())('documents bucket RLS', () => {
  let member: TestUser;
  let outsider: TestUser;
  let teamId: string;

  beforeAll(async () => {
    member = await createUser('st-mem');
    outsider = await createUser('st-out');
    teamId = await createTeam(member, []);
  });

  afterAll(async () => {
    await cleanupTeam(teamId, [member, outsider]);
  });

  test('a member uploads into their own team folder', async () => {
    const { error } = await member.client.storage
      .from('documents')
      .upload(`${teamId}/spec.txt`, 'hello', { contentType: 'text/plain' });
    expect(error).toBeNull();
  });

  test('a non-member cannot upload into that folder', async () => {
    const { error } = await outsider.client.storage
      .from('documents')
      .upload(`${teamId}/intruder.txt`, 'nope', { contentType: 'text/plain' });
    expect(error).not.toBeNull();
  });

  test('a non-member cannot list the folder', async () => {
    const { data } = await outsider.client.storage.from('documents').list(teamId);
    expect(data ?? []).toEqual([]);
  });

  test('the bucket is private — no public URL serves the object', async () => {
    const { data } = member.client.storage
      .from('documents')
      .getPublicUrl(`${teamId}/spec.txt`);
    const res = await fetch(data.publicUrl);
    expect(res.ok).toBe(false);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm run test:integration -- storage`
Expected: FAIL — upload errors with `Bucket not found`.

- [ ] **Step 3: Write the migration**

Create `supabase/migrations/20260721090000_storage_documents.sql`:

```sql
-- The documents bucket and its RLS.
--
-- Storage was the one surface with no policies at all: the frontend uploaded
-- to a bucket that did not exist, and hand-creating that bucket without
-- policies would have made every team's documents readable by any
-- authenticated user of the project.
--
-- Upload paths are `{team_id}/{uuid}-{filename}` (frontend Documents.tsx), so
-- the first path segment is the tenant key. Reads go through signed URLs
-- (createSignedUrl), so the bucket stays private.

insert into storage.buckets (id, name, public)
values ('documents', 'documents', false)
on conflict (id) do nothing;

-- SELECT + INSERT only: the app never updates or deletes objects. Document
-- deletion is soft (documents.deleted_at) and leaves the file in place.
--
-- ponytail: the ::uuid cast raises rather than denies on a malformed first
-- segment. Nothing can store such a path (the same cast guards INSERT), so
-- only a service_role write could create one. Swap to a text comparison
-- against the caller's team ids if that ever happens.
create policy st_documents_select on storage.objects for select to authenticated
  using (
    bucket_id = 'documents'
    and public.is_team_member(((storage.foldername(name))[1])::uuid)
  );

create policy st_documents_insert on storage.objects for insert to authenticated
  with check (
    bucket_id = 'documents'
    and public.is_team_member(((storage.foldername(name))[1])::uuid)
  );
```

- [ ] **Step 4: Apply and run the test**

Run: `npx supabase migration up` then `cd frontend && npm run test:integration -- storage`
Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add supabase/migrations/20260721090000_storage_documents.sql frontend/tests/integration/storage.test.ts
git commit -m "fix: provision documents bucket with tenant-scoped storage RLS"
```

---

### Task 2: Diff-card suppression guard

**Files:**
- Modify: `server/app.py` (the SELECT inside `observation_suppress`, and its 404 message)
- Test: `tests/test_consent_tiers.py` (append — the existing suppression DB tests live here)

**Interfaces:**
- Consumes: existing `observation_suppress` endpoint, `public.memory_compilations.diff_message_id`.
- Produces: no new symbols.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_consent_tiers.py`:

```python
def test_suppress_refuses_a_memory_diff_card(seeded):
    """Diff cards are notifications, not observations — silencing them would
    break the post-hoc transparency the memory model depends on."""
    from fastapi.testclient import TestClient

    from server.app import app
    from server.auth import current_user_id

    conn = _admin()
    try:
        msg_id = conn.execute(
            "insert into public.messages (team_id, thread_type, sender_kind, body)"
            " values (%s,'group','ai','Memory updated — 2 added, 1 revised, 0 removed.')"
            " returning id",
            (TEAM_A,),
        ).fetchone()[0]
        conn.execute(
            "insert into public.memory_compilations (team_id, trigger, status,"
            " diff_message_id) values (%s,'on_demand','done',%s)",
            (TEAM_A, msg_id),
        )
    finally:
        conn.close()

    app.dependency_overrides[current_user_id] = lambda: A1
    try:
        resp = TestClient(app).post(
            f"/observations/{msg_id}/suppress",
            json={"team_id": TEAM_A, "kind": "proactive_observation"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404
    conn = _admin()
    try:
        deleted, suppressions = conn.execute(
            "select (select deleted_at from public.messages where id=%(m)s),"
            " (select count(*) from public.observation_suppressions"
            "  where message_id=%(m)s)",
            {"m": msg_id},
        ).fetchone()
    finally:
        conn.close()
    assert deleted is None            # card still visible
    assert suppressions == 0          # nothing recorded


def test_suppress_still_works_on_a_plain_observation(seeded):
    from fastapi.testclient import TestClient

    from server.app import app
    from server.auth import current_user_id

    conn = _admin()
    try:
        msg_id = conn.execute(
            "insert into public.messages (team_id, thread_type, sender_kind, body)"
            " values (%s,'group','ai','Observation: the doc has not moved.')"
            " returning id",
            (TEAM_A,),
        ).fetchone()[0]
    finally:
        conn.close()

    app.dependency_overrides[current_user_id] = lambda: A1
    try:
        resp = TestClient(app).post(
            f"/observations/{msg_id}/suppress",
            json={"team_id": TEAM_A, "kind": "proactive_observation"},
        )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_consent_tiers.py -k suppress -q`
Expected: `test_suppress_refuses_a_memory_diff_card` FAILS with `assert 200 == 404`.

- [ ] **Step 3: Add the guard**

In `server/app.py`, inside `observation_suppress`, change the insert's SELECT from:

```python
            " select %s, %s, %s, m.id from public.messages m"
            " where m.id=%s and m.team_id=%s and m.sender_kind='ai'"
            " and m.thread_type='group'"
            " returning id",
```

to:

```python
            " select %s, %s, %s, m.id from public.messages m"
            " where m.id=%s and m.team_id=%s and m.sender_kind='ai'"
            " and m.thread_type='group'"
            # Diff cards are notifications, not observations: silencing them
            # would break the transparency that replaces a memory approval gate.
            " and not exists (select 1 from public.memory_compilations c"
            "                 where c.diff_message_id = m.id)"
            " returning id",
```

and change the 404 detail from `"not an AI group message in this team"` to
`"not a suppressible AI observation in this team"`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_consent_tiers.py -q`
Expected: all pass (18 tests).

- [ ] **Step 5: Commit**

```bash
git add server/app.py tests/test_consent_tiers.py
git commit -m "fix: memory diff cards cannot be suppressed as observations"
```

---

### Task 3: Per-team hourly turn budget on /agent/turn

**Files:**
- Modify: `shared/config.py` (add `agent_turns_per_hour`)
- Modify: `server/app.py` (add `_check_turn_budget`, call it in `agent_turn`)
- Modify: `.env.example` (document the knob)
- Test: `tests/test_server_budget.py` (create)

**Interfaces:**
- Consumes: `settings.agent_turns_per_hour: int`, `user_session(user_id)`, `require_membership(user_id, team_id)`.
- Produces: `server.app._check_turn_budget(user_id: str, team_id: str) -> None` — raises `HTTPException(429)` when the team is at or over its hourly cap; returns `None` otherwise.

- [ ] **Step 1: Write the failing test**

Create `tests/test_server_budget.py`:

```python
"""Per-team hourly turn budget: the lid on the LLM spend hole."""
import psycopg
import pytest
from fastapi.testclient import TestClient

from server.app import app
from server.auth import current_user_id
from shared.config import settings
from tests._seed import A1, B1, TEAM_A


def _admin():
    conn = psycopg.connect(settings.comrade_db_url_admin)
    conn.autocommit = True
    return conn


def _seed_runs(n: int, *, minutes_ago: int = 5) -> None:
    conn = _admin()
    try:
        for _ in range(n):
            conn.execute(
                "insert into public.agent_runs (team_id, trigger_type, status,"
                " created_at) values (%s,'user','done', now() - make_interval(mins => %s))",
                (TEAM_A, minutes_ago),
            )
    finally:
        conn.close()


@pytest.fixture
def as_a1():
    app.dependency_overrides[current_user_id] = lambda: A1
    yield TestClient(app)
    app.dependency_overrides.clear()


def _turn(client):
    return client.post("/agent/turn", json={"team_id": TEAM_A, "text": "status?"})


def test_turn_is_refused_once_the_team_hits_its_cap(seeded, as_a1, monkeypatch):
    monkeypatch.setattr(settings, "agent_turns_per_hour", 2)
    _seed_runs(2)
    resp = _turn(as_a1)
    assert resp.status_code == 429
    assert "agent turns" in resp.json()["detail"]


def test_turn_is_allowed_below_the_cap(seeded, as_a1, monkeypatch):
    """Budget check must not itself run the agent — stub the orchestrator."""
    monkeypatch.setattr(settings, "agent_turns_per_hour", 5)
    monkeypatch.setattr(
        "server.app.run_turn_sync",
        lambda *a, **k: {"run_id": "r", "reply": "ok", "steps": []},
    )
    monkeypatch.setattr("server.app._persist_user_message", lambda *a: "m1")
    monkeypatch.setattr("server.app._persist_ai_reply", lambda *a: "m2")
    _seed_runs(1)
    assert _turn(as_a1).status_code == 200


def test_old_runs_fall_out_of_the_window(seeded, as_a1, monkeypatch):
    monkeypatch.setattr(settings, "agent_turns_per_hour", 1)
    monkeypatch.setattr(
        "server.app.run_turn_sync",
        lambda *a, **k: {"run_id": "r", "reply": "ok", "steps": []},
    )
    monkeypatch.setattr("server.app._persist_user_message", lambda *a: "m1")
    monkeypatch.setattr("server.app._persist_ai_reply", lambda *a: "m2")
    _seed_runs(3, minutes_ago=120)      # yesterday's spend doesn't count
    assert _turn(as_a1).status_code == 200


def test_zero_disables_the_cap(seeded, as_a1, monkeypatch):
    monkeypatch.setattr(settings, "agent_turns_per_hour", 0)
    monkeypatch.setattr(
        "server.app.run_turn_sync",
        lambda *a, **k: {"run_id": "r", "reply": "ok", "steps": []},
    )
    monkeypatch.setattr("server.app._persist_user_message", lambda *a: "m1")
    monkeypatch.setattr("server.app._persist_ai_reply", lambda *a: "m2")
    _seed_runs(50)
    assert _turn(as_a1).status_code == 200


def test_non_member_gets_403_not_429(seeded, monkeypatch):
    """Never leak that a team exists, or how busy it is, to an outsider."""
    monkeypatch.setattr(settings, "agent_turns_per_hour", 1)
    _seed_runs(5)
    app.dependency_overrides[current_user_id] = lambda: B1
    try:
        resp = TestClient(app).post(
            "/agent/turn", json={"team_id": TEAM_A, "text": "hi"}
        )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_server_budget.py -q`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'agent_turns_per_hour'`.

- [ ] **Step 3: Add the config knob**

In `shared/config.py`, inside the `Settings` class, after `cors_origins`:

```python
    # Per-team hourly cap on agent turns — the lid on LLM spend and the
    # simplest abuse brake. 0 disables the cap entirely.
    agent_turns_per_hour: int = 60
```

- [ ] **Step 4: Add the check and wire it in**

In `server/app.py`, add above `@app.post("/agent/turn", ...)`:

```python
def _check_turn_budget(user_id: str, team_id: str) -> None:
    """Refuse the turn when the team is at its hourly cap.

    agent_runs already records every turn (team_id, created_at), so the limit
    is a count over a table that exists: no new store, correct across API
    instances, and it survives a restart. idx_agent_runs_team covers the
    query. Runs as the member, so RLS scopes the count to their own team.
    """
    cap = settings.agent_turns_per_hour
    if cap <= 0:
        return
    with user_session(user_id) as conn:
        used = conn.execute(
            "select count(*) from public.agent_runs"
            " where team_id=%s and created_at > now() - interval '1 hour'",
            (team_id,),
        ).fetchone()[0]
    if used >= cap:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"This team has used its {cap} agent turns for the hour."
            " Comrade will be available again shortly.",
        )
```

Then in `agent_turn`, immediately after `require_membership(user_id, req.team_id)`:

```python
    _check_turn_budget(user_id, req.team_id)
```

- [ ] **Step 5: Document the knob**

In `.env.example`, under the `# --- HTTP surface ---` block, after `CORS_ORIGINS`:

```
# Per-team hourly cap on agent turns (LLM spend lid). 0 disables it.
AGENT_TURNS_PER_HOUR=60
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_server_budget.py -q`
Expected: `5 passed`.

- [ ] **Step 7: Full suite + commit**

Run: `uv run pytest -q` → expect `162 passed, 4 deselected`.

```bash
git add shared/config.py server/app.py .env.example tests/test_server_budget.py
git commit -m "feat: per-team hourly agent-turn budget returns 429 at the cap"
```

---

### Task 4: Update HANDOFF gap list

**Files:**
- Modify: `HANDOFF.md` (§3 API table, §6 gap list)

- [ ] **Step 1: Add the 429 to the API table**

In §3, append to the `POST /agent/turn` row's Notes: `Returns **429** when the team is over its hourly turn cap (AGENT_TURNS_PER_HOUR, default 60).`

- [ ] **Step 2: Record what remains**

In §6, remove nothing (none of the listed gaps were closed by this slice) and add:

```markdown
7. **Rate limiting covers `/agent/turn` only** — team-scoped, turn-count-based
   (not tokens). Other endpoints are unlimited; they are cheap RLS'd DB writes.
8. **Storage RLS is tenant-scoped by path prefix** — uploads MUST keep the
   `{team_id}/{uuid}-{filename}` shape or the policy will reject them.
```

- [ ] **Step 3: Commit**

```bash
git add HANDOFF.md
git commit -m "docs: record turn budget and storage path contract"
```

---

## Self-review

- **Spec coverage:** Hole 1 → Task 1. Hole 2 → Task 2. Hole 3 → Task 3. Spec's testing section (3 tests) → Tasks 1/2/3 each carry theirs; Task 3 carries the 403-beats-429 case explicitly. Spec's "skipped" table needs no task by definition.
- **Placeholders:** none — every code step shows the code.
- **Type consistency:** `_check_turn_budget(user_id, team_id) -> None` is declared once in Task 3's Interfaces block and used with that exact signature in Step 4. Column is `created_at` in every occurrence (spec corrected). Bucket id is `documents` in both the migration and the test.
