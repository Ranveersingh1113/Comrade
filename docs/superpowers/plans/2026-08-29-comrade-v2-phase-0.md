# Comrade v2 — Phase 0: Demolish and Stabilise

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> Parent plan: [`2026-08-29-comrade-v2.md`](2026-08-29-comrade-v2.md). Source of reasoning: `docs/agent-architecture-findings-2026-08-12.md`.

**Goal:** Fix one live bug, delete three decided-away capabilities, and land the four cheapest high-value corrections — ending with a smaller, faster, more correct system and **no new capability**.

**Architecture:** Every task is a deletion, a constraint, or a projection change. Nothing new is built. The one exception is Task 1, which repairs a group-reply path that RLS currently blocks.

**Tech Stack:** Python 3.12 · psycopg3 · Postgres/Supabase RLS · pytest · React 19 + Vitest

## Global Constraints

Inherited verbatim from the parent plan's Global Constraints section. The two that bind every task here:

1. **Migration policy (§8, §16.5):** any index on an *existing* table uses `CREATE INDEX CONCURRENTLY` (outside a transaction); any check constraint on an existing table is added `NOT VALID` then `VALIDATE CONSTRAINT`.
2. **Reads borrow the requester; writes act as the app** (§4.1).

## Phase-wide prerequisites

- [ ] A local Supabase is running and migrations are applied (`supabase db reset` or `supabase migration up`).
- [ ] `.env` has all five DB URLs. `uv run pytest` is green before starting.
- [ ] Working on branch `feat/comrade-v2-phase-0`, cut from `feat/frontend`.

## Task order and why

| # | Task | Rationale for position |
|---|---|---|
| 1 | Repair blocked AI group reply (+ record migration policy) | 🔴 Live bug. The headline feature is broken in production. First. |
| 2 | Delete `page_index()` | Trivial deletion; shrinks `pipeline/wiki.py` before Tasks 9–10 rewrite it. |
| 3 | Suppress endpoint validates its target | Isolated, one file. |
| 4 | Unique `action_hash` on pending | One migration; must land before consent code is edited in Tasks 5–7. |
| 5 | Remove T3 + two-key — backend | §13.7: doing §10 first removes most of §13's test work for free. |
| 6 | Remove T3 + two-key — frontend | Follows 5 so the API it calls is already gone. |
| 7 | Remove `team_propose_group_message` | After 5 (§13.7), before Task 8 so Task 8 refactors one fewer tool. |
| 8 | Reads run as the requester | The phase's only real refactor. Everything above shrinks its surface first. |
| 9 | Temporal + source annotation at render | Largest measured effect in the document (§20.3.1); ~15 lines. |
| 10 | Write page descriptions | Feeds the live recall index (§20.4-1). |
| 11 | Connection pool | Pure infra swap, zero semantic change. Last so it pools the *final* call pattern. |
| 12 | Correct stale records | Documentation truth after the code truth changed. |

---

## Task 1: Repair the blocked AI group reply  ✅ DONE (710ea7c)

🔴 **New finding, not in the findings doc.** `server/app.py:_persist_ai_reply` inserts `thread_type='group'` under `Role.AGENT`, but `ag_messages_insert` (`20260612120000_action_consent.sql:33`) has `with check (... and thread_type = 'private' and sender_kind = 'ai')`. A group `@comrade` turn's reply therefore **fails at RLS in production**.

No test catches it: `tests/test_server.py` and `tests/test_server_stream.py` both monkeypatch `_persist_ai_reply`, and `tests/test_runtime_live.py` calls `run_turn` directly without going through the endpoint.

§13.6 of the findings doc asserts the opposite ("`_persist_ai_reply` … inserts into `messages` with `thread_type='group'` … ungated"). That is true at the **GRANT** level and false at the **POLICY** level. Correct it in Task 12.

The load-bearing invariant is `sender_kind = 'ai'` — the agent must never impersonate a member. Thread type was never the guard.

**Files:**
- Create: `supabase/migrations/20260829090000_agent_group_reply.sql`
- Create: `tests/test_rls_agent_reply.py`
- Modify: `docs/architecture.md` (append the migration policy — F32)

**Interfaces:**
- Consumes: `shared.db.Role`, `shared.db.team_session`, `tests._seed.{TEAM_A, A1}`
- Produces: nothing importable. Later tasks rely on the policy `ag_messages_insert` permitting `sender_kind='ai'` at any `thread_type`.

- [x] **Step 1: Write the failing test**

Create `tests/test_rls_agent_reply.py`:

```python
"""The agent's one kept group-visible write: its reply to an @comrade turn.

§13.1 keeps exactly two group-visible AI writes — the direct reply, and the
compiler's diff card. ag_messages_insert restricted the agent to private
threads, so the direct reply failed at RLS. Every HTTP test stubs
_persist_ai_reply, which is why nothing caught it.
"""
import psycopg
import pytest

from shared.db import Role, team_session
from tests._seed import A1, TEAM_A


def test_agent_can_insert_a_group_reply(seeded):
    with team_session(Role.AGENT, TEAM_A) as conn:
        row = conn.execute(
            "insert into public.messages (team_id, thread_type, thread_owner_id,"
            " sender_kind, body)"
            " values (%s,'group',null,'ai','The demo is Friday.') returning id",
            (TEAM_A,),
        ).fetchone()
    assert row is not None


def test_agent_can_still_insert_a_private_nudge(seeded):
    with team_session(Role.AGENT, TEAM_A) as conn:
        row = conn.execute(
            "insert into public.messages (team_id, thread_type, thread_owner_id,"
            " sender_kind, body)"
            " values (%s,'private',%s,'ai','Checking in.') returning id",
            (TEAM_A, A1),
        ).fetchone()
    assert row is not None


def test_agent_cannot_impersonate_a_member(seeded):
    """The invariant the policy actually protects: never a human's name."""
    with pytest.raises(psycopg.Error):
        with team_session(Role.AGENT, TEAM_A) as conn:
            conn.execute(
                "insert into public.messages (team_id, thread_type,"
                " sender_kind, sender_id, body)"
                " values (%s,'group','user',%s,'not actually me')",
                (TEAM_A, A1),
            )
```

- [x] **Step 2: Run the tests to verify the first one fails**

Run: `uv run pytest tests/test_rls_agent_reply.py -v`
Expected: `test_agent_can_insert_a_group_reply` FAILS with a `new row violates row-level security policy for table "messages"`. The other two PASS.

If the first test *passes*, stop and report — the policy is not what the migration file says, and the rest of this task is unnecessary.

- [x] **Step 3: Write the migration**

Create `supabase/migrations/20260829090000_agent_group_reply.sql`:

```sql
-- ============================================================
-- MIGRATION POLICY (findings doc §8, §16.5) — in force from here onward
-- ------------------------------------------------------------
--   * Any index on an EXISTING table uses CREATE INDEX CONCURRENTLY, in its
--     own migration file with no surrounding transaction.
--   * Any check constraint on an EXISTING table is added NOT VALID first,
--     then VALIDATE CONSTRAINT as a separate statement.
-- ============================================================

-- Fix: the agent's reply to an explicit @comrade invocation is one of the two
-- group-visible AI writes kept by §13.1, but ag_messages_insert
-- (20260612120000_action_consent.sql:33) required thread_type='private'.
-- server/app.py:_persist_ai_reply inserts thread_type='group' on a group turn,
-- so that path failed at RLS.
--
-- The invariant this policy exists to protect is sender_kind='ai': the agent
-- may never write a message attributed to a human. Thread type was never the
-- guard, and restricting it broke the product's headline interaction.
--
-- The agent still holds NO update on messages (20260612120000:27), so it can
-- write an AI message and never edit one.

drop policy if exists ag_messages_insert on public.messages;
create policy ag_messages_insert on public.messages for insert to comrade_agent
  with check (team_id = public.current_team() and sender_kind = 'ai');
```

- [x] **Step 4: Apply the migration and re-run**

Run: `supabase migration up && uv run pytest tests/test_rls_agent_reply.py -v`
Expected: all three PASS.

- [x] **Step 5: Record the migration policy in the architecture doc**

Append to `docs/architecture.md`:

```markdown
## Migration policy

In force from `20260829090000_agent_group_reply.sql` onward (findings doc §8, §16.5):

- Any index on an **existing** table uses `CREATE INDEX CONCURRENTLY`, in its own
  migration file with no surrounding transaction.
- Any check constraint on an **existing** table is added `NOT VALID` first, then
  `VALIDATE CONSTRAINT` as a separate statement.

Prior violations, left in place because the tables were small at the time:
`20260719090000_production_hardening.sql:10` (non-concurrent index on `jobs`) and
`20260719130000_consent_tiers.sql` (direct check-constrained columns on
`consent_queue`).
```

- [x] **Step 6: Run the full suite**

Run: `uv run pytest`
Expected: all PASS.

- [x] **Step 7: Commit**

```bash
git add supabase/migrations/20260829090000_agent_group_reply.sql tests/test_rls_agent_reply.py docs/architecture.md
git commit -m "fix: let the agent persist its reply in the group room

ag_messages_insert required thread_type='private', so the reply to an
@comrade invocation failed at RLS. The invariant is sender_kind='ai', not
the thread type. Adds the three RLS tests that were missing because every
HTTP test stubs _persist_ai_reply.

Also records the CONCURRENTLY / NOT VALID migration policy (findings §8)."
```

---

## Task 2: Delete `page_index()`

§2.4, §6.3-10. `pipeline/wiki.py:58` is called only by its own test; `agent/agent.py:wiki_section` builds the same projection inline. §20.4 reframes this: the *function* is dead, the *projection* is live — so delete the function and leave the live path alone.

**Files:**
- Modify: `pipeline/wiki.py` (delete `page_index`, fix the module docstring)
- Modify: `tests/test_wiki.py` (delete the import and `test_page_index_is_titles_and_descriptions`)

**Interfaces:**
- Consumes: nothing.
- Produces: `pipeline.wiki` exports only `ORPHAN_TITLE`, `all_active_pages`, `render_team_wiki` after this task.

- [ ] **Step 1: Prove nothing else calls it**

Run: `grep -rn "page_index" --include=*.py --include=*.ts --include=*.tsx --include=*.md .`
Expected: exactly four hits — `pipeline/wiki.py:7` (docstring), `pipeline/wiki.py:58` (definition), `tests/test_wiki.py:4` (import), `tests/test_wiki.py:75` (call). Any other hit means stop and re-scope.

- [ ] **Step 2: Delete the test**

In `tests/test_wiki.py`, change line 4 from:

```python
from pipeline.wiki import ORPHAN_TITLE, all_active_pages, page_index, render_team_wiki
```

to:

```python
from pipeline.wiki import ORPHAN_TITLE, all_active_pages, render_team_wiki
```

and delete the whole of `test_page_index_is_titles_and_descriptions` (lines 66–77).

- [ ] **Step 3: Run the test file to verify it now fails on the still-present function's absence of coverage — i.e. verify it simply passes**

Run: `uv run pytest tests/test_wiki.py -v`
Expected: PASS with one fewer test. (This is a deletion, so there is no red step — the guard is Step 1's grep.)

- [ ] **Step 4: Delete the function and correct the docstring**

In `pipeline/wiki.py`, delete lines 58–63:

```python
def page_index(conn, team_id: str) -> list[dict]:
    """Titles + descriptions only — the cheap recall index."""
    return [
        {"title": p["title"], "description": p["description"]}
        for p in all_active_pages(conn, team_id)
    ]
```

and in the module docstring replace:

```
  - page_index(): titles + descriptions only — the future agent-recall index
    (LLM selector picks pages by description, Claude-Code style).
```

with:

```
  (The recall index is NOT here: agent/agent.py:wiki_section builds the
  titles+descriptions projection inline and injects it into the system prompt
  every turn. page_index() was a duplicate of that and was deleted 2026-08-29.)
```

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add pipeline/wiki.py tests/test_wiki.py
git commit -m "refactor: delete dead page_index()

Called only by its own test. The live recall index is built inline by
agent/agent.py:wiki_section (findings §2.4, reframed by §20.4)."
```

---

## Task 3: The suppress endpoint validates its target

§2.5. `POST /observations/{id}/suppress` already refuses memory diff cards, but it does not check that the message is a *proactive observation* — any AI group message can be tombstoned. §12.3 notes nothing produces proactive observations yet, so the endpoint's whole target class is currently "every AI group message."

The minimum correct fix without inventing a producer: validate the `kind` against a known set, so a caller cannot record an arbitrary suppression string that no future producer will ever match.

**Files:**
- Modify: `server/app.py` (`SuppressRequest`, `observation_suppress`)
- Modify: `tests/test_server.py` (add one test — its `client` fixture already stubs `require_membership` and the budget)

**Interfaces:**
- Consumes: `fastapi.HTTPException`, `pydantic.field_validator`
- Produces: `server.app.SUPPRESSIBLE_KINDS: frozenset[str]`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_server.py`. Body validation fires before the handler runs, so this needs no DB at all — which is why it belongs here rather than in the DB-backed suppress tests:

```python
def test_suppress_rejects_an_unknown_kind(client):
    """A kind no producer will ever emit is a typo, not a preference (§2.5)."""
    resp = client.post(
        "/observations/00000000-0000-0000-0000-000000000001/suppress",
        json={"team_id": TEAM, "kind": "not_a_real_kind"},
    )
    assert resp.status_code == 422


def test_suppress_accepts_the_known_kind(client):
    """The known kind passes validation; only the DB lookup can reject it.

    404 (no such observation) proves the request body was accepted and the
    handler ran. A 422 would mean validation wrongly rejected a real kind.
    """
    resp = client.post(
        "/observations/00000000-0000-0000-0000-000000000001/suppress",
        json={"team_id": TEAM, "kind": "proactive_observation"},
    )
    assert resp.status_code != 422
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_server.py -k suppress -v`
Expected: `test_suppress_rejects_an_unknown_kind` FAILS — validation passes, so the handler runs and the response is not 422. `test_suppress_accepts_the_known_kind` PASSES already.

- [ ] **Step 3: Constrain the kind**

In `server/app.py`, replace:

```python
class SuppressRequest(TeamScoped):
    kind: str
```

with:

```python
# The categories a proactive AI observation can belong to. §12.3: nothing
# produces these yet, so the set is the contract a future producer must match
# — an unvalidated free-text kind records a preference nothing will ever read.
SUPPRESSIBLE_KINDS = frozenset({"proactive_observation"})


class SuppressRequest(TeamScoped):
    kind: str

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, v: str) -> str:
        if v not in SUPPRESSIBLE_KINDS:
            raise ValueError(
                f"unknown observation kind {v!r};"
                f" expected one of {sorted(SUPPRESSIBLE_KINDS)}"
            )
        return v
```

and add `field_validator` to the pydantic import at the top:

```python
from pydantic import BaseModel, Field, field_validator
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_server.py tests/test_consent_tiers.py -k suppress -v`
Expected: all suppress tests PASS, including the two DB-backed ones in `test_consent_tiers.py` (they already send `kind='proactive_observation'`).

- [ ] **Step 5: Commit**

```bash
git add server/app.py tests/test_consent_tiers.py
git commit -m "fix: validate the observation kind on suppress

findings §2.5. An unvalidated kind records a 'don't do this again' signal
that no producer will ever match. §12.3: no producer exists yet, so the
allowed set IS the contract the future one must satisfy."
```

---

## Task 4: Unique `action_hash` on pending proposals

§2.2. `action_hash` is computed and stored but has no unique constraint, so a retried turn silently creates a duplicate consent card. Live now.

**Files:**
- Create: `supabase/migrations/20260829091000_consent_hash_unique.sql`
- Modify: `tests/test_consent.py` (add one test)

**Interfaces:**
- Consumes: `shared.consent.propose_action`
- Produces: a partial unique index `uq_consent_pending_hash` that later tasks must not violate. `propose_action` raises `psycopg.errors.UniqueViolation` on a duplicate pending proposal from this task onward.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_consent.py`:

```python
def test_identical_pending_proposal_is_rejected(seeded):
    """A retried turn must not produce two identical consent cards (§2.2)."""
    import psycopg

    args = {"assignee_id": A1, "title": "write the report",
            "description": None, "deadline": None}
    propose_action(TEAM_A, A1, "task_create", args)
    with pytest.raises(psycopg.errors.UniqueViolation):
        propose_action(TEAM_A, A1, "task_create", args)
```

Ensure the file's imports include `pytest`, `propose_action`, `A1` and `TEAM_A`; add whichever are missing.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_consent.py::test_identical_pending_proposal_is_rejected -v`
Expected: FAIL — `DID NOT RAISE`, because the second insert succeeds.

- [ ] **Step 3: Write the migration**

Create `supabase/migrations/20260829091000_consent_hash_unique.sql`:

```sql
-- findings §2.2. action_hash is computed at propose time (shared/consent.py)
-- and re-verified at execute time, but nothing stopped a retried turn from
-- writing the same proposal twice. The only index on the table was
-- idx_consent_queue_team_status.
--
-- Scoped to status='pending': a resolved item is history and may legitimately
-- repeat later (the same action proposed again next week is a new decision).
--
-- CONCURRENTLY per the migration policy — consent_queue is an existing table.
-- Run this file outside a transaction.

create unique index concurrently if not exists uq_consent_pending_hash
  on public.consent_queue (team_id, action_hash)
  where status = 'pending';
```

**Note for the implementer:** Supabase's migration runner wraps files in a transaction by default. If `CREATE INDEX CONCURRENTLY` errors with "cannot run inside a transaction block", split this into its own file and apply it with `psql -f`, or use the `-- supabase:no-transaction` directive if your CLI version supports it. Do not silently drop `CONCURRENTLY` — record the workaround in the file's comment header.

- [ ] **Step 4: Apply and re-run**

Run: `supabase migration up && uv run pytest tests/test_consent.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add supabase/migrations/20260829091000_consent_hash_unique.sql tests/test_consent.py
git commit -m "fix: unique action_hash on pending consent items

findings §2.2. A retried turn silently created a duplicate proposal.
Partial index on status='pending' so a genuinely repeated action later is
still allowed."
```

---

## Task 5: Remove T3 and the two-key system — backend

§10, owner decision 2026-08-12. Reverses the provisional governance ruling in `20260719130000_consent_tiers.sql`. Collapses O1: the consent queue then holds exactly one shape, "needs the requester's key."

**Decision Q1 (settled): `tier` survives**, narrowed to `('T0','T1','T2')`. It stays an informational label, keeps `_TOOL_TIER_FLOORS` meaningful for the future autonomy classifier (G4/G5), and narrowing a check is a cheaper migration to reverse than dropping a column.

**Files:**
- Create: `supabase/migrations/20260829092000_remove_t3.sql`
- Modify: `shared/consent.py` (delete `add_second_key`; the T3 branches in `execute_consent` and `approve_consent`; `"T3"` from `_TIER_ORDER`)
- Modify: `server/app.py` (delete `POST /consent/{id}/second_key` and the `add_second_key` import)
- Modify: `tests/test_consent_tiers.py` (delete the T3 gate + countersign-integrity blocks; keep the floor tests)
- Modify: `tests/test_server.py` (delete `test_second_key_passes_the_caller`, `test_second_key_not_countersignable_is_404`)

**Interfaces:**
- Consumes: nothing new.
- Produces: `shared.consent` no longer exports `add_second_key`. `approve_consent` always returns `execute_consent`'s result — there is no `{"status": "approved", "awaiting": "second_key"}` shape any more. Task 6 relies on this.

- [ ] **Step 1: Delete the tests that assert T3 behaviour**

In `tests/test_consent_tiers.py`:
- Change the import block to drop `add_second_key`:

```python
from shared.consent import (
    ConsentError, approve_consent, execute_consent, propose_action, resolve_tier,
)
```

- Delete `_propose_t3` (lines 19–26).
- Delete these tests entirely: `test_tier_can_be_raised`, `test_t3_without_second_key_does_not_execute`, `test_t3_executes_once_both_keys_land`, `test_t3_second_key_before_approval_waits`, `test_direct_execute_still_blocked_without_second_key`, `test_requester_cannot_countersign_their_own_item`, `test_requester_cannot_forge_the_second_key_column`, `test_countersigner_cannot_touch_anything_else`, `test_editing_args_voids_an_existing_countersign`, `test_teammates_can_see_pending_t3_items`, `test_teammates_still_cannot_see_lower_tier_items`.
- Keep: `test_tier_floors_cannot_be_lowered`, `test_unknown_tool_defaults_conservatively`, `test_suppression_rls_member_writes_own_only`, `test_opens_summary_counts_without_naming`, `test_tombstone_fn_marks_only_ai_messages_and_is_agent_only`, `test_suppress_refuses_a_memory_diff_card`, `test_suppress_still_works_on_a_plain_observation`, `test_suppress_rejects_an_unknown_kind` (from Task 3).

In `tests/test_server.py`, delete `test_second_key_passes_the_caller` and `test_second_key_not_countersignable_is_404` (lines 165–183).

- [ ] **Step 2: Add the replacement test that pins the new invariant**

Append to `tests/test_consent_tiers.py`:

```python
def test_tier_cannot_be_t3_any_more(seeded):
    """§10: T3 is gone. A caller asking for it gets the tool's floor instead."""
    assert resolve_tier("task_create", "T3") == "T1"
    cid = propose_action(
        TEAM_A, A1, "task_create",
        {"assignee_id": A1, "title": "x", "description": None, "deadline": None},
        tier="T3",
    )["consent_id"]
    conn = _admin()
    try:
        row = conn.execute(
            "select tier from public.consent_queue where id=%s", (cid,)
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "T1"
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/test_consent_tiers.py::test_tier_cannot_be_t3_any_more -v`
Expected: FAIL — `resolve_tier("task_create", "T3")` still returns `"T3"`.

- [ ] **Step 4: Edit `shared/consent.py`**

Replace the tier block:

```python
# Blast-radius tiers (governance ruling, provisional): T0 read-only, T1
# affects one member, T2 shared+reversible, T3 external/irreversible/money.
_TIER_ORDER = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}
```

with:

```python
# Blast-radius tiers: T0 read-only, T1 affects one member, T2 shared and
# reversible. T3 (external/irreversible/money) and its two-key countersign
# were removed by owner decision 2026-08-12 (findings §10) — for code, GitHub
# branch protection is a stronger second key than the trigger ever was
# (§16.2). `tier` survives as an informational label and as the seed for the
# earned-trust ratchet (§9.3 G4/G5).
_TIER_ORDER = {"T0": 0, "T1": 1, "T2": 2}
```

Delete the whole `add_second_key` function.

In `execute_consent`, change the CAS `returning` clause from:

```python
            " returning tool_name, tool_args, action_hash, requesting_member_id,"
            " expires_at, tier, second_approver_id",
```

to:

```python
            " returning tool_name, tool_args, action_hash, requesting_member_id,"
            " expires_at",
```

and the unpack from:

```python
        (tool_name, args, action_hash, requester_id, expires_at,
         tier, second_approver_id) = claimed
```

to:

```python
        (tool_name, args, action_hash, requester_id, expires_at) = claimed
```

and delete the T3 hard-gate block:

```python
        # T3 hard gate: two keys, and the second is never the initiator (the
        # DB check constraint backs this; re-checked here so the error is a
        # clean ConsentError rollback, not a constraint failure).
        if tier == "T3" and second_approver_id is None:
            raise ConsentError(
                f"consent {consent_id} is T3 and needs a second key"
                " from another member"
            )
```

Replace `approve_consent` entirely with:

```python
def approve_consent(team_id: str, consent_id: str, approver_id: str) -> dict:
    """Requester approves a pending item; it executes immediately.

    Since §10 removed T3, approval is always the last key. There is no
    waiting state.
    """
    with user_session(approver_id) as conn:
        row = conn.execute(
            "update public.consent_queue set status='approved'"
            " where id=%s and status='pending' returning id",
            (consent_id,),
        ).fetchone()
    if row is None:
        return {"status": "not_approved", "reason": "not pending or not yours"}
    return execute_consent(team_id, consent_id)
```

- [ ] **Step 5: Edit `server/app.py`**

Change the consent import from:

```python
from shared.consent import (
    ConsentError, add_second_key, approve_consent, edit_and_approve,
    reject_consent,
)
```

to:

```python
from shared.consent import (
    ConsentError, approve_consent, edit_and_approve, reject_consent,
)
```

and delete the whole `consent_second_key` route (the `@app.post("/consent/{consent_id}/second_key")` decorator and its function).

- [ ] **Step 6: Write the migration**

Create `supabase/migrations/20260829092000_remove_t3.sql`:

```sql
-- findings §10, owner decision 2026-08-12. T3 and the two-key countersign are
-- removed. This reverses the provisional governance ruling in
-- 20260719130000_consent_tiers.sql.
--
-- Rationale, narrower than "two-key was overhead" (§24.1): for code, GitHub
-- branch protection is a stronger second key than this trigger, enforced by
-- the system that owns the resource (§16.2). The non-code case is left
-- uncovered, knowingly.
--
-- `tier` SURVIVES, narrowed to T0-T2 (decision Q1). It stays meaningful for
-- _TOOL_TIER_FLOORS and seeds the earned-trust ratchet (§9.3 G4/G5).
-- Narrowing a check is cheaper to reverse than dropping a column.

drop policy if exists au_consent_queue_select_t3 on public.consent_queue;
drop policy if exists au_consent_queue_second_key on public.consent_queue;

drop trigger if exists trg_consent_second_key on public.consent_queue;
drop function if exists public.trg_consent_second_key_guard();

alter table public.consent_queue
  drop constraint if exists consent_second_key_distinct;

alter table public.consent_queue
  drop column if exists second_approver_id,
  drop column if exists second_approved_at;

-- Narrow the tier check. NOT VALID then VALIDATE per the migration policy;
-- any existing T3 row must be rewritten first or VALIDATE will fail.
update public.consent_queue set tier = 'T2' where tier = 'T3';

alter table public.consent_queue
  drop constraint if exists consent_queue_tier_check;
alter table public.consent_queue
  add constraint consent_queue_tier_check
  check (tier in ('T0','T1','T2')) not valid;
alter table public.consent_queue
  validate constraint consent_queue_tier_check;
```

**Note for the implementer:** confirm the existing check constraint's real name first —
`select conname from pg_constraint where conrelid = 'public.consent_queue'::regclass and contype = 'c';`
Postgres auto-names it, and `consent_queue_tier_check` is the *expected* name, not a guaranteed one. Substitute the real name if it differs.

- [ ] **Step 7: Apply and run**

Run: `supabase migration up && uv run pytest tests/test_consent.py tests/test_consent_tiers.py tests/test_consent_loop.py tests/test_server.py -v`
Expected: all PASS.

- [ ] **Step 8: Run the full backend suite**

Run: `uv run pytest`
Expected: all PASS. Frontend is still red at this point — Task 6 fixes it.

- [ ] **Step 9: Commit**

```bash
git add supabase/migrations/20260829092000_remove_t3.sql shared/consent.py server/app.py tests/
git commit -m "feat!: remove T3 and the two-key countersign (backend)

findings §10, owner decision 2026-08-12. The consent queue now holds one
shape: needs the requester's key. Drops two policies, one trigger, one
function, one constraint and two columns; deletes add_second_key and the
POST /consent/{id}/second_key route.

tier survives, narrowed to T0-T2 (decision Q1) — it seeds the earned-trust
ratchet in §9.3 G4/G5.

BREAKING: POST /consent/{id}/second_key is gone. Frontend follows next commit."
```

---

## Task 6: Remove T3 and the two-key system — frontend

Continues Task 5 across the UI layer. §10.1's removal manifest, re-verified 2026-08-29.

**Files:**
- Modify: `frontend/src/lib/consentModel.ts` (drop three phases and the T3 block)
- Modify: `frontend/src/lib/agentApi.ts` (delete `secondKeyConsent`)
- Modify: `frontend/src/lib/types.ts` (narrow `ConsentTier`, drop `second_approver_id`)
- Modify: `frontend/src/components/ConsentCard.tsx` (three badges, the countersign button block)
- Modify: `frontend/src/screens/ConsentInbox.tsx` (the T3 comment, the "still live" clause, the legend row)
- Modify: `frontend/tests/unit/consentModel.test.ts`
- Modify: `frontend/tests/component/ConsentCard.test.tsx`
- Modify: `frontend/tests/e2e/global-setup.ts` (the seeded T3 item), `frontend/tests/e2e/journeys.spec.ts` (the two-context countersign journey), `frontend/tests/integration/rls-consent.test.ts`

**Interfaces:**
- Consumes: `shared.consent`'s post-Task-5 shape — `approve_consent` never returns a waiting state.
- Produces: `ConsentPhase` is `'pending' | 'executed' | 'rejected' | 'cancelled' | 'stale'`. `ConsentTier` is `'T0' | 'T1' | 'T2'`. `ConsentItem` has no `second_approver_id`.

- [ ] **Step 1: Update the unit test first**

In `frontend/tests/unit/consentModel.test.ts`, delete every test whose name mentions T3, countersign, or second key. Then append:

```typescript
it('has no countersign phase after the T3 removal', () => {
  const item = {
    ...base,
    tier: 'T2' as const,
    status: 'approved' as const,
    requesting_member_id: 'someone-else',
  };
  // A teammate viewing another member's item sees it as pending, not as
  // something they can countersign — findings §10.
  expect(consentPhase(item, 'me', false)).toBe('pending');
});
```

Adjust `base` to whatever the file's existing fixture object is named; if there is none, inline a full `ConsentItem` literal.

- [ ] **Step 2: Run it to verify it fails**

Run: `cd frontend && npx vitest run tests/unit/consentModel.test.ts`
Expected: FAIL — TypeScript still has `second_approver_id` on the fixture, or the assertion returns `'can_countersign'`.

- [ ] **Step 3: Rewrite `consentModel.ts`**

Replace the whole file's phase machinery:

```typescript
// Consent-card state machine. Tiers grade by blast radius (T0-T2) and are
// informational only — every item needs exactly one key, the requester's
// (findings §10, owner decision 2026-08-12). This module only decides what
// the viewer is shown; the backend decides what may happen.
import type { ConsentItem } from './types';

export type ConsentPhase =
  | 'pending' // actionable by the requester
  | 'executed'
  | 'rejected'
  | 'cancelled'
  | 'stale';

export function consentPhase(
  item: ConsentItem,
  _viewerId: string | null,
  staleFromApi: boolean,
): ConsentPhase {
  if (staleFromApi) return 'stale';
  if (item.status === 'executed') return 'executed';
  if (item.status === 'rejected') return 'rejected';
  if (item.status === 'cancelled') return 'cancelled';
  return 'pending';
}
```

Keep `argsPretty` exactly as it is.

- [ ] **Step 4: Update `types.ts`**

Replace lines 174–175 and the `second_approver_id` field:

```typescript
/** Blast-radius tier — informational since the T3 removal (findings §10). */
export type ConsentTier = 'T0' | 'T1' | 'T2';
```

and delete `second_approver_id: string | null;` from `ConsentItem`.

- [ ] **Step 5: Delete `secondKeyConsent` from `agentApi.ts`**

Delete lines 92–95:

```typescript
/** T3 second key: a teammate (never the requester) countersigns. */
export function secondKeyConsent(consentId: string, teamId: string) {
  return request<ConsentActionResult>(`/consent/${consentId}/second_key`, { team_id: teamId });
}
```

- [ ] **Step 6: Strip the T3 UI from `ConsentCard.tsx`**

Delete the three T3 badge branches (lines 41–46) so the badge ternary falls through to its existing non-T3 cases, and delete the countersign block (lines 323–345 — the `phase === 'can_countersign'`, `awaiting_second_key` and `countersigned_pending` branches). Remove the now-unused `secondKeyConsent` import.

**Also note in a comment above the `REVERSIBLE` badge** (around line 128), per §13.3-2:

```tsx
{/* After §13 removed team_propose_group_message, every proposal is
    reversible=true — task_create is the only registered tool and it sets
    it. The IRREVERSIBLE branch is cosmetically dead until a genuinely
    irreversible tool is registered. The column stays: the audit trigger
    reads it (20260719130000:79,:83). */}
```

- [ ] **Step 7: Strip the T3 logic from `ConsentInbox.tsx`**

Delete the T3 comment at lines 17–18, replace the "still live" clause at lines 33–38 with:

```typescript
  // Every item needs exactly one key — the requester's (findings §10).
  const isLive = (i: ConsentItem) => i.status === 'pending';
```

and delete the T3 legend row at line 75.

- [ ] **Step 8: Fix the remaining fixtures**

- `frontend/tests/component/ConsentCard.test.tsx`: remove `second_approver_id` from fixtures; change any `tool_name: 'post_group_message'` literal to `'task_create'` (§13.5 follow-up 1 — no test should name a removed capability).
- `frontend/tests/e2e/global-setup.ts`: delete the seeded T3 item (around line 80).
- `frontend/tests/e2e/journeys.spec.ts`: delete the two-context countersign journey (around line 91).
- `frontend/tests/integration/rls-consent.test.ts`: replace the `post_group_message` literals (lines 23, 30, 83) with `task_create`.

- [ ] **Step 9: Typecheck and run**

Run: `cd frontend && npm run build && npm test`
Expected: `tsc -b` clean, all vitest PASS.

- [ ] **Step 10: Run the integration and e2e suites**

Run: `cd frontend && npm run test:integration && npm run test:e2e`
Expected: all PASS.

- [ ] **Step 11: Commit**

```bash
git add frontend/
git commit -m "feat!: remove T3 and the two-key countersign (frontend)

findings §10. consentPhase drops four of its eight phases — the 8-phase
state machine was the symptom of tier meaning living in four layers (O1).
Removes secondKeyConsent, the three T3 badges, the countersign block and
the inbox legend row.

Also switches surviving fixtures off post_group_message so no test names a
capability being removed in the next commit (§13.5)."
```

---

## Task 7: Remove `team_propose_group_message`

§13, owner decision 2026-08-17. The agent's ability to *originate* group content is removed. The two kept cases are not exceptions — neither is the agent acting by itself.

⚠️ **§13.5 is the main risk in this task.** Of ten references to `post_group_message`, only two go red, because `propose_action` performs no `tool_name` validation. **Do not trust a green suite here.** The tool-name validation that would make this fail loudly arrives in Phase 1 (F8, the tool registry); until then, verify each deletion by grep.

**Files:**
- Create: `supabase/migrations/20260829093000_revoke_executor_messages.sql`
- Modify: `agent/tools.py` (delete `team_propose_group_message`)
- Modify: `agent/agent.py` (import, tools list, instruction rewrite)
- Modify: `shared/consent.py` (the floor, `_precheck_post_group_message`, `_exec_post_group_message`, both dict entries)
- Modify: `tests/test_agent.py`, `tests/test_consent_loop.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `agent.tools` exports four tools — `team_get_state`, `memory_read_page`, `team_propose_task`, `member_send_nudge`. `shared.consent._EXECUTORS` holds one key, `task_create`.

- [ ] **Step 1: Update the tests to expect the new tool set**

In `tests/test_agent.py`, change the expected-tools assertion (line ~17) to:

```python
    assert {t.__name__ for t in root_agent.tools} == {
        "team_get_state",
        "memory_read_page",
        "team_propose_task",
        "member_send_nudge",
    }
```

In `tests/test_consent_loop.py`, delete `test_propose_group_message_approve_posts` entirely (lines 72–98).

- [ ] **Step 2: Add the test that pins the removal**

Append to `tests/test_consent_loop.py`:

```python
def test_post_group_message_has_no_executor(seeded):
    """§13: the agent may not originate group content. Nothing executes it."""
    from shared.consent import _EXECUTORS

    assert "post_group_message" not in _EXECUTORS
    assert set(_EXECUTORS) == {"task_create"}
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/test_agent.py tests/test_consent_loop.py -v`
Expected: `test_tools_registered` FAILS (five tools, four expected) and `test_post_group_message_has_no_executor` FAILS.

- [ ] **Step 4: Delete the tool**

In `agent/tools.py`, delete the whole `team_propose_group_message` function.

In `agent/agent.py`:
- drop `team_propose_group_message` from the `from agent.tools import (...)` block;
- drop it from the `tools=[...]` list;
- replace the "Taking action" section of `INSTRUCTION`:

```
Taking action:
- To create a task, use team_propose_task. To post to the group room, use
  team_propose_group_message. Both are proposals, not done deals — they go to a
  human for approval. Say you've proposed it, not that it's done.
- To check in with a member privately, use member_send_nudge. It sends right
  away; keep it to the situations the nudge types describe.
- You never post to the group or create tasks directly; gated actions always go
  through a proposal a human approves.
```

with:

```
Taking action:
- To create a task, use team_propose_task. It is a proposal, not a done deal —
  it goes to a human for approval. Say you've proposed it, not that it's done.
- To check in with a member privately, use member_send_nudge. It sends right
  away; keep it to the situations the nudge types describe.
- You never post to the group room on your own initiative. When someone asks
  you in the room, your answer goes there because they asked. If a member
  wants something said to the team, they say it themselves — offer to draft it
  for them and let them send it under their own name.
```

- [ ] **Step 5: Delete the executor and precheck**

In `shared/consent.py`:
- delete `"post_group_message": "T2",` from `_TOOL_TIER_FLOORS`;
- delete `_precheck_post_group_message` and `_exec_post_group_message` entirely;
- reduce the two dicts to:

```python
_PRECHECKS = {
    "task_create": _precheck_task_create,
}
_EXECUTORS = {
    "task_create": _exec_task_create,
}
```

- [ ] **Step 6: Write the migration**

Create `supabase/migrations/20260829093000_revoke_executor_messages.sql`:

```sql
-- findings §13.3-1. team_propose_group_message was the only consent tool whose
-- executor wrote to `messages`. With it removed, _EXECUTORS holds only
-- task_create, whose executor writes `tasks` and nothing else — so
-- `grant insert on public.messages to comrade_executor`
-- (20260612120000_action_consent.sql:45) is dead privilege.
--
-- Least privilege is the whole point of the role split; a granted-but-unused
-- write is exactly the thing that silently becomes reachable again later.

revoke insert on public.messages from comrade_executor;
drop policy if exists ex_messages_insert on public.messages;
```

- [ ] **Step 7: Apply and run**

Run: `supabase migration up && uv run pytest`
Expected: all PASS.

- [ ] **Step 8: Verify by grep, not by green suite**

Run: `grep -rn "post_group_message\|propose_group_message" --include=*.py --include=*.ts --include=*.tsx --include=*.sql .`
Expected: **zero hits** outside `docs/`. If any test or fixture still names it, fix it now — §13.5 is explicit that eight of ten such references keep passing while referring to a capability that no longer exists.

- [ ] **Step 9: Run the frontend suites**

Run: `cd frontend && npm test && npm run test:integration && npm run test:e2e`
Expected: all PASS (Task 6 already switched the fixtures).

- [ ] **Step 10: Commit**

```bash
git add agent/ shared/consent.py supabase/migrations/20260829093000_revoke_executor_messages.sql tests/
git commit -m "feat!: the agent never initiates a group post

findings §13, owner decision 2026-08-17. Removes
team_propose_group_message, its executor, its precheck and its tier floor,
and revokes comrade_executor's now-dead insert on messages (§13.3-1).

Root cause worth recording: the tool had no trigger condition to remove —
agent.py said only 'to post to the group room, use it', with no when and no
scenario. Removing an ungoverned capability is cheaper than inventing the
governance it never had.

The two kept group-visible AI writes are unaffected: the reply to an
explicit @comrade turn, and the compiler's diff card."
```

---

## Task 8: Reads run as the requester

§2.1 (🔴 the latent private-thread leak), §4.1 (the design decision), independently confirmed by §6.1 (PromptQL) and §9.1 (Claude Code). **The single most important change in Phase 0.**

The agent's reads move from `Role.AGENT` (team-scoped, sees every member's private thread) to `user_session(requester_id)` (the member's own RLS). Writes stay on `Role.AGENT`. This is a *deletion* of grants, not an addition of policies.

**Critical detail:** under `Role.AGENT`, `current_team()` scoped every query, so the SQL carried no `team_id` filter. Under `authenticated`, a member can see **every team they belong to**. Every read query must gain an explicit `team_id` filter or it will leak across the requester's own teams.

**Known and accepted side effect** (§4.1): `team_get_state`'s `open_consent` will return only the *caller's own* pending items, because that is what `au_consent_queue_select` allows. That is more correct than the current behaviour.

**Files:**
- Create: `supabase/migrations/20260829094000_agent_read_scope.sql`
- Modify: `agent/tools.py` (`fetch_team_state`, `read_memory_page`, and both ADK wrappers)
- Modify: `agent/agent.py` (`wiki_section`, `build_instruction`)
- Modify: `shared/db.py` (docstring only — `user_session` already exists and is correct)
- Modify: `tests/test_tools.py`, `tests/test_agent_memory.py`
- Create: `tests/test_agent_read_scope.py`

**Interfaces:**
- Consumes: `shared.db.user_session(user_id)` — already present at `shared/db.py:74`.
- Produces:
  - `fetch_team_state(team_id: str, requester_id: str) -> dict`
  - `read_memory_page(team_id: str, requester_id: str, title: str) -> dict`
  - `agent.agent.wiki_section(team_id: str, requester_id: str) -> str`

  All three take `requester_id` as their **second** positional argument. Phase 1's tool registry and Phase 2's read tools depend on this signature.

- [ ] **Step 1: Write the failing test**

Create `tests/test_agent_read_scope.py`:

```python
"""The agent reads as the member who invoked it, never as itself.

findings §2.1: ag_messages was team-scoped, not thread-scoped, so the agent
role could select every member's private thread. §4.1's fix is to delete the
agent's read grants rather than add policies — reads run under the
requester's own RLS.
"""
import psycopg
import pytest

from agent.tools import fetch_team_state, read_memory_page
from shared.db import Role, team_session
from tests._seed import A1, A2, TEAM_A


def test_agent_role_can_no_longer_read_messages(seeded):
    """The grant is gone, so the leak is structurally impossible."""
    with pytest.raises(psycopg.Error):
        with team_session(Role.AGENT, TEAM_A) as conn:
            conn.execute("select body from public.messages").fetchall()


def test_agent_role_can_still_insert_its_own_reply(seeded):
    """Writes stay on Role.AGENT — only reads moved (§4.1)."""
    with team_session(Role.AGENT, TEAM_A) as conn:
        row = conn.execute(
            "insert into public.messages (team_id, thread_type, thread_owner_id,"
            " sender_kind, body)"
            " values (%s,'group',null,'ai','still works') returning id",
            (TEAM_A,),
        ).fetchone()
    assert row is not None


def test_team_state_reads_as_the_requester(seeded):
    state = fetch_team_state(TEAM_A, A1)
    assert state["team"]["id"] == TEAM_A
    assert {m["user_id"] for m in state["members"]} == {A1, A2}


def test_memory_page_reads_as_the_requester(seeded):
    """A member can read the team wiki; the projection is unchanged."""
    result = read_memory_page(TEAM_A, A1, "Uncategorized")
    assert "error" not in result or result["error"] == "no such page"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_agent_read_scope.py -v`
Expected: `test_agent_role_can_no_longer_read_messages` FAILS (the select succeeds), and the two `requester` tests FAIL with `TypeError: fetch_team_state() takes 1 positional argument but 2 were given`.

- [ ] **Step 3: Rewrite `fetch_team_state`**

In `agent/tools.py`, replace the whole function. Note every added `where` clause — under `authenticated` there is no `current_team()` to scope by:

```python
def fetch_team_state(team_id: str, requester_id: str) -> dict:
    """Snapshot a team's coordination state, read as the requesting member.

    findings §4.1: the agent borrows the requester's permissions rather than
    holding its own. Every query carries an explicit team_id because
    `authenticated` can see every team the member belongs to — there is no
    current_team() to scope by, unlike the worker roles.

    Consequence, and correct: open_consent returns only the caller's OWN
    pending items, because that is what au_consent_queue_select allows.
    """
    with user_session(requester_id) as conn:
        team = conn.execute(
            "select id, name from public.teams where id = %s", (team_id,)
        ).fetchone()
        if team is None:
            return {"error": "team not found or not accessible"}

        members = conn.execute(
            "select m.user_id, p.display_name, m.role"
            " from public.memberships m"
            " join public.profiles p on p.id = m.user_id"
            " where m.team_id = %s and m.status = 'active'"
            " order by m.role, p.display_name",
            (team_id,),
        ).fetchall()

        tasks = conn.execute(
            "select id, title, assignee_id, status, deadline"
            " from public.tasks where team_id = %s and status <> 'done'"
            " order by deadline nulls last",
            (team_id,),
        ).fetchall()

        consent = conn.execute(
            "select id, tool_name, requesting_member_id"
            " from public.consent_queue"
            " where team_id = %s and status = 'pending'"
            " order by created_at",
            (team_id,),
        ).fetchall()

    return {
        "team": {"id": str(team[0]), "name": team[1]},
        "members": [
            {"user_id": str(r[0]), "display_name": r[1], "role": r[2]}
            for r in members
        ],
        "tasks": [
            {
                "id": str(r[0]),
                "title": r[1],
                "assignee_id": str(r[2]) if r[2] else None,
                "status": r[3],
                "deadline": r[4].isoformat() if r[4] else None,
            }
            for r in tasks
        ],
        "open_consent": [
            {
                "id": str(r[0]),
                "tool_name": r[1],
                "requesting_member_id": str(r[2]) if r[2] else None,
            }
            for r in consent
        ],
    }
```

- [ ] **Step 4: Rewrite `read_memory_page`**

Change its signature and session, leaving the body's logic untouched (`all_active_pages` already filters on `team_id` explicitly):

```python
def read_memory_page(team_id: str, requester_id: str, title: str) -> dict:
    """One wiki page's active facts with their citations, read as the member.

    Titles match case-insensitively — the model reads them off an index, so a
    capitalisation slip should not read as "no such page".
    """
    with user_session(requester_id) as conn:
```

Everything from `pages = [p for p in all_active_pages(...)]` down stays exactly as it is.

- [ ] **Step 5: Update the imports and the two ADK wrappers**

At the top of `agent/tools.py`, change:

```python
from shared.db import Role, team_session
```

to:

```python
from shared.db import user_session
```

(`Role` and `team_session` are no longer used in this module — `send_nudge` and `propose_action` open their own sessions.)

Then update the wrappers:

```python
def team_get_state(tool_context: ToolContext) -> dict:
    """Get the current team's state: members, live tasks, and pending consent
    items. Call this before summarising status or referencing who/what exists."""
    return fetch_team_state(
        tool_context.state["team_id"], tool_context.state["requester_id"]
    )


def memory_read_page(title: str, tool_context: ToolContext) -> dict:
    """Read one page of the team wiki: its facts and where each came from.

    Use this before answering about decisions, deadlines, scope, or history.
    The page titles are listed in your instructions. Cite what you find. If a
    page does not contain the answer, say the wiki does not record it.

    Args:
        title: a page title from the wiki index in your instructions.
    """
    return read_memory_page(
        tool_context.state["team_id"], tool_context.state["requester_id"], title
    )
```

- [ ] **Step 6: Update `wiki_section` in `agent/agent.py`**

```python
def wiki_section(team_id: str, requester_id: str) -> str:
    """The team wiki's page index — titles and descriptions only.

    Claude Code's model: the index is always in context, page bodies load on
    demand (memory_read_page). Pages with no active facts are omitted so the
    agent never opens an empty one.

    Read as the requesting member (findings §4.1), not as the agent role.
    """
    with user_session(requester_id) as conn:
        pages = [p for p in all_active_pages(conn, team_id) if p["facts"]]
```

The rest of the function is unchanged. Then:

```python
def build_instruction(ctx: ReadonlyContext) -> str:
    """Per-turn instruction: static rules + this team's wiki index."""
    return INSTRUCTION + wiki_section(
        ctx.state["team_id"], ctx.state["requester_id"]
    )
```

and change the import line from:

```python
from shared.db import Role, team_session
```

to:

```python
from shared.db import user_session
```

- [ ] **Step 7: Write the migration**

Create `supabase/migrations/20260829094000_agent_read_scope.sql`:

```sql
-- findings §2.1 + §4.1. The agent borrows the requester's permissions for
-- reads; it keeps its own identity only for writes.
--
-- §2.1 (🔴): ag_messages_select was team-scoped, not thread-scoped, and
-- comrade_agent held select on public.messages. Nothing exposed messages to
-- the model yet, so it was latent — it goes live the moment messages_search
-- exists (Phase 2). Fixing it by adding a thread_owner_id clause would be the
-- wrong shape: the agent should not have a private read surface at all.
--
-- Independently reached by two external systems: PromptQL ("if you can't see
-- it, the agent can't see it for you", §6.1) and Claude Code, which has no
-- agent identity at all — every decision is made against the user's rules
-- (§9.1).
--
-- REMAINING agent reads, and why each survives:
--   nudge_log   — the 24h cooldown lookup in shared/nudge.py, a write-path read
--   agent_runs  — shared/agent_runs.py:get_run, the agent's own audit trail
--   change_log  — the audit trigger writes it under this role
-- Everything else is now read through user_session().

revoke select on
  public.profiles, public.teams, public.memberships, public.messages,
  public.documents, public.document_opens,
  public.memory_compilations, public.memory_entries, public.memory_versions,
  public.memory_citations, public.memory_reverts,
  public.tasks, public.milestones, public.github_repos, public.github_activity,
  public.consent_queue
from comrade_agent;

revoke select on public.memory_pages from comrade_agent;
revoke select on public.observation_suppressions from comrade_agent;

-- The policies are now unreachable (no grant to gate). Drop them so the next
-- reader is not misled into thinking the agent still has a read surface.
drop policy if exists ag_profiles              on public.profiles;
drop policy if exists ag_teams                 on public.teams;
drop policy if exists ag_memberships           on public.memberships;
drop policy if exists ag_messages_select       on public.messages;
drop policy if exists ag_documents             on public.documents;
drop policy if exists ag_document_opens        on public.document_opens;
drop policy if exists ag_memory_compilations   on public.memory_compilations;
drop policy if exists ag_memory_entries        on public.memory_entries;
drop policy if exists ag_memory_versions       on public.memory_versions;
drop policy if exists ag_memory_citations      on public.memory_citations;
drop policy if exists ag_memory_reverts        on public.memory_reverts;
drop policy if exists ag_memory_pages          on public.memory_pages;
drop policy if exists ag_tasks                 on public.tasks;
drop policy if exists ag_milestones            on public.milestones;
drop policy if exists ag_github_repos          on public.github_repos;
drop policy if exists ag_github_activity       on public.github_activity;

-- consent_queue: the agent still INSERTS proposals, so its policy stays but
-- narrows to insert only. It never needed to read the queue back.
drop policy if exists ag_consent_queue on public.consent_queue;
create policy ag_consent_queue_insert on public.consent_queue
  for insert to comrade_agent
  with check (team_id = public.current_team());
```

**Note for the implementer:** `20260612110000_fix_profiles_policy.sql` re-creates `ag_profiles`; the `drop policy if exists` above handles whichever version is live.

- [ ] **Step 8: Fix the existing tests that call the old signatures**

Run: `grep -rn "fetch_team_state\|read_memory_page\|wiki_section" tests/ scripts/ evaluation/`

Every call site gains a `requester_id` second argument. In `tests/test_tools.py` and `tests/test_agent_memory.py`, use `A1` from `tests._seed`. In `scripts/smoke_agent.py`, use the seeded user id the script already creates.

- [ ] **Step 9: Apply and run**

Run: `supabase migration up && uv run pytest tests/test_agent_read_scope.py tests/test_tools.py tests/test_agent_memory.py tests/test_rls_isolation.py -v`
Expected: all PASS.

- [ ] **Step 10: Run the full suite**

Run: `uv run pytest`
Expected: all PASS.

- [ ] **Step 11: Live check**

Run: `uv run pytest -m live tests/test_agent_memory_live.py -v`
Expected: PASS — the agent still answers from the wiki, now reading as the member.

- [ ] **Step 12: Commit**

```bash
git add agent/ shared/db.py supabase/migrations/20260829094000_agent_read_scope.sql tests/ scripts/
git commit -m "feat!: the agent reads as the requester, not as itself

findings §2.1 + §4.1. Closes the latent private-thread leak by DELETING the
agent's read grants rather than adding a thread_owner_id clause — the agent
should have no private read surface at all.

Every read query gains an explicit team_id filter: under authenticated
there is no current_team() to scope by, and a member can see every team
they belong to.

Accepted consequence (§4.1): team_get_state's open_consent now returns only
the caller's own pending items, which is what au_consent_queue_select
allows and is more correct than the previous behaviour.

Writes stay on Role.AGENT. Independently reached by PromptQL (§6.1) and
Claude Code (§9.1).

BREAKING: fetch_team_state, read_memory_page and wiki_section all take
requester_id as their second positional argument."
```

---

## Task 9: Temporal and source annotation at render

§20.3.1. **The largest measured effect of anything in the findings doc** — an 80% vs 41% temporal-reasoning gap turning on whether facts reach the reader carrying dates. Comrade stores `valid_from`, `created_at`, `change_type` and `source_kind`, and throws all of it away at `pipeline/wiki.py:76`. No schema change; the columns exist and `all_active_pages` already joins the table they live on.

**Files:**
- Modify: `pipeline/wiki.py` (`all_active_pages` SQL and projection, `render_team_wiki`)
- Modify: `pipeline/compiler.py` (`build_consolidation_prompt`)
- Modify: `tests/test_wiki.py`, `tests/test_compiler.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: each fact dict from `all_active_pages` gains two keys — `valid_from: datetime` and `source_kind: str | None`. Task 10 and Phase 2's `memory_search` both read this shape.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_wiki.py`:

```python
def test_facts_carry_their_date_and_source(seeded):
    """findings §20.3.1: an undated bullet is the widest measured gap."""
    conn = _admin()
    try:
        with conn.cursor() as cur:
            _seed_page_with_fact(cur, TEAM_A, "Deadlines", "Demo is Friday",
                                 description="key dates")
    finally:
        conn.close()
    with team_session(Role.PIPELINE, TEAM_A) as s:
        pages = all_active_pages(s, TEAM_A)
    fact = next(f for p in pages for f in p["facts"] if f["text"] == "Demo is Friday")
    assert fact["valid_from"] is not None
    assert "source_kind" in fact


def test_render_annotates_facts_with_date_and_source(seeded):
    conn = _admin()
    try:
        with conn.cursor() as cur:
            _seed_page_with_fact(cur, TEAM_A, "Deadlines", "Demo is Friday",
                                 description="key dates")
    finally:
        conn.close()
    with team_session(Role.PIPELINE, TEAM_A) as s:
        md = render_team_wiki(s, TEAM_A)
    assert "Demo is Friday" in md
    assert "as of " in md
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_wiki.py -k "date_and_source or annotates" -v`
Expected: FAIL — `KeyError: 'valid_from'` and `assert "as of " in md`.

- [ ] **Step 3: Widen the fact query**

In `pipeline/wiki.py`, replace the `fact_rows` query and the loop that builds `item`:

```python
    fact_rows = conn.execute(
        "select e.page_id, e.id, v.fact, v.valid_from,"
        "       (select c.source_kind from public.memory_citations c"
        "         where c.version_id = v.id order by c.created_at limit 1)"
        " from public.memory_entries e"
        " join public.memory_versions v on v.entry_id = e.id and v.is_active"
        " where e.team_id = %s and not e.archived order by v.created_at",
        (team_id,),
    ).fetchall()

    by_page: dict[str, list[dict]] = {}
    orphans: list[dict] = []
    for page_id, entry_id, fact, valid_from, source_kind in fact_rows:
        item = {
            "entry_id": str(entry_id),
            "text": fact,
            "valid_from": valid_from,
            "source_kind": source_kind,
        }
        if page_id is None:
            orphans.append(item)
        else:
            by_page.setdefault(str(page_id), []).append(item)
```

- [ ] **Step 4: Add the shared annotation helper and use it in rendering**

Add to `pipeline/wiki.py`, above `render_team_wiki`:

```python
_SOURCE_LABEL = {"message": "from chat", "document": "from a doc", "github": "from the repo"}


def annotate(fact: dict) -> str:
    """One fact as a bullet carrying its date and provenance.

    findings §20.3.1: the measured temporal-reasoning gap (80% vs 41%) turns
    on whether facts reach the reader dated. A fact compiled this morning and
    one compiled in May must not look identical — the reader cannot otherwise
    prioritise what to re-verify against live state.
    """
    bits = []
    if fact.get("valid_from") is not None:
        bits.append(f"as of {fact['valid_from']:%Y-%m-%d}")
    label = _SOURCE_LABEL.get(fact.get("source_kind") or "")
    if label:
        bits.append(label)
    suffix = f"  _({', '.join(bits)})_" if bits else ""
    return f"- {fact['text']}{suffix}"
```

and in `render_team_wiki` replace:

```python
        bullets = "\n".join(f"- {f['text']}" for f in p["facts"])
```

with:

```python
        bullets = "\n".join(annotate(f) for f in p["facts"])
```

- [ ] **Step 5: Annotate the consolidation prompt too**

In `pipeline/compiler.py`, add `annotate` to the wiki import:

```python
from pipeline.wiki import all_active_pages, annotate
```

and in `build_consolidation_prompt` replace:

```python
        listed = "\n".join(
            f"- [{f['entry_id']}] {f['text']}" for f in p["facts"]
        ) or "(no facts yet)"
```

with:

```python
        listed = "\n".join(
            f"- [{f['entry_id']}] {annotate(f)[2:]}" for f in p["facts"]
        ) or "(no facts yet)"
```

(`annotate` returns a `"- "`-prefixed bullet; the `[2:]` strips that prefix so the entry id stays first, which is what the consolidator matches on.)

Then extend `_CONSOLIDATE_SYSTEM` — append to the end of the string, before the closing paren:

```python
    " Each existing fact is shown with the date it became true and where it"
    " came from; prefer 'revise' over 'add' when a candidate updates an older"
    " fact, and weigh a recent fact above a stale one when they conflict."
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_wiki.py tests/test_compiler.py -v`
Expected: all PASS. If a compiler test asserts an exact prompt string, update the expected value to the annotated form.

- [ ] **Step 7: Update the agent's wiki index too**

In `agent/agent.py`, `wiki_section` currently lists titles and descriptions only — that stays correct (it is an *index*, not a body). No change. Confirm by re-reading the function.

- [ ] **Step 8: Run the full suite**

Run: `uv run pytest`
Expected: all PASS.

- [ ] **Step 9: Commit**

```bash
git add pipeline/wiki.py pipeline/compiler.py tests/
git commit -m "feat: facts carry their date and source into the model

findings §20.3.1 — the largest measured effect in the document. The
benchmark's widest gap is temporal reasoning (80% vs 41%) and the dividing
factor is whether facts reach the reader carrying dates.

Comrade stored valid_from, change_type and source_kind and discarded all of
it at wiki.py:76. No schema change; the columns already existed.

Also feeds the consolidator, which can now prefer 'revise' over 'add' and
weigh a recent fact above a stale one."
```

---

## Task 10: Write page descriptions

§2.3, promoted by §20.4-1. `memory_pages.description` is read in six places and written in **zero** — so the live recall index that `agent/agent.py:wiki_section` injects on every turn is titles-only. The selector is running on the weakest input it could have.

Stage 2 already has the whole wiki in context and already picks a target page per added fact; asking it for a one-line description when it proposes a *new* title is near-zero marginal cost.

**Files:**
- Modify: `pipeline/compiler.py` (`Decision`, `_CONSOLIDATE_SYSTEM`, `validate_decisions`, `_resolve_page`, `apply_compilation`'s call site)
- Modify: `tests/test_compiler.py`

**Interfaces:**
- Consumes: `Decision` from Task 9's unchanged shape.
- Produces: `Decision` gains `page_description: str | None`. `_resolve_page(conn, team_id, title, description)` takes a fourth argument.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_compiler.py`:

```python
def test_a_new_page_gets_its_description_written(seeded):
    """findings §2.3 + §20.4-1: descriptions feed the LIVE recall index."""
    candidates = [Candidate(text="We ship on Fridays", excerpt="ship Fridays")]
    decisions = [
        Decision(candidate_index=0, action="add", page_title="Release Cadence",
                 page_description="how and when we ship")
    ]
    with team_session(Role.PIPELINE, TEAM_A) as conn:
        apply_compilation(conn, TEAM_A, candidates, decisions, [None])
        row = conn.execute(
            "select description from public.memory_pages"
            " where team_id=%s and title='Release Cadence'",
            (TEAM_A,),
        ).fetchone()
    assert row[0] == "how and when we ship"


def test_validate_normalises_a_blank_description():
    candidates = [Candidate(text="x")]
    raw = [Decision(candidate_index=0, action="add", page_title="P",
                    page_description="   ")]
    out = validate_decisions(candidates, [], raw)
    assert out[0].page_description is None
```

Add `validate_decisions` to the file's imports from `pipeline.compiler` if it is not already there.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_compiler.py -k description -v`
Expected: FAIL — `Decision` has no `page_description` field.

- [ ] **Step 3: Extend the `Decision` schema**

In `pipeline/compiler.py`:

```python
class Decision(BaseModel):
    candidate_index: int
    action: str  # 'add' | 'revise' | 'invalidate' | 'noop'
    entry_id: str | None = None
    page_title: str | None = None  # for 'add': target page (existing or new)
    # For 'add' onto a NEW page: one line saying what the page is for. This is
    # the input the LIVE recall index selects on (agent/agent.py:wiki_section),
    # not routing polish — findings §2.3, promoted by §20.4-1.
    page_description: str | None = None
```

- [ ] **Step 4: Ask for it in the prompt**

In `_CONSOLIDATE_SYSTEM`, change:

```
" 'add' (genuinely new information - also set page_title to the existing"
" page it belongs on, or propose a short new page title of 2-4 words),"
```

to:

```
" 'add' (genuinely new information - also set page_title to the existing"
" page it belongs on, or propose a short new page title of 2-4 words; when"
" you propose a NEW title, also set page_description to one short line"
" saying what belongs on that page, so a reader can pick it from an index"
" without opening it),"
```

- [ ] **Step 5: Normalise it in `validate_decisions`**

In the `out.append(...)` block, change:

```python
        title = (d.page_title or "").strip() or None
        out.append(
            Decision(
                candidate_index=i, action=d.action,
                entry_id=d.entry_id, page_title=title,
            )
        )
```

to:

```python
        title = (d.page_title or "").strip() or None
        description = (d.page_description or "").strip() or None
        out.append(
            Decision(
                candidate_index=i, action=d.action,
                entry_id=d.entry_id, page_title=title,
                page_description=description,
            )
        )
```

- [ ] **Step 6: Persist it in `_resolve_page`**

Replace the function:

```python
def _resolve_page(conn, team_id: str, title: str | None, description: str | None = None):
    """Find (case-insensitively) or create the page an added fact lands on.

    An existing page's description is filled in if it is still blank, but
    never overwritten — the first compiler to name a page wins, and a later
    document should not silently rewrite what the page is for.
    """
    name = (title or "").strip() or DEFAULT_PAGE_TITLE
    desc = (description or "").strip()
    row = conn.execute(
        "select id, description from public.memory_pages"
        " where team_id=%s and lower(title)=lower(%s)",
        (team_id, name),
    ).fetchone()
    if row is not None:
        if desc and not row[1]:
            conn.execute(
                "update public.memory_pages set description=%s where id=%s",
                (desc, row[0]),
            )
        return row[0]
    created = conn.execute(
        "insert into public.memory_pages (team_id, title, description)"
        " values (%s,%s,%s) on conflict do nothing returning id",
        (team_id, name, desc),
    ).fetchone()
    if created is not None:
        return created[0]
    # Another compilation inserted the same page between our lookup and insert.
    return conn.execute(
        "select id from public.memory_pages"
        " where team_id=%s and lower(title)=lower(%s)",
        (team_id, name),
    ).fetchone()[0]
```

- [ ] **Step 7: Pass the description at the call site**

Run: `grep -n "_resolve_page" pipeline/compiler.py`

In `apply_compilation`, change the call from `_resolve_page(conn, team_id, d.page_title)` to `_resolve_page(conn, team_id, d.page_title, d.page_description)`.

- [ ] **Step 8: Run the tests**

Run: `uv run pytest tests/test_compiler.py -v`
Expected: all PASS.

- [ ] **Step 9: Run the full suite plus a live compile**

Run: `uv run pytest && uv run pytest -m live tests/test_compiler_live.py -v`
Expected: all PASS, and the live compile produces at least one page with a non-empty description.

- [ ] **Step 10: Commit**

```bash
git add pipeline/compiler.py tests/test_compiler.py
git commit -m "feat: the compiler writes page descriptions

findings §2.3, promoted by §20.4-1. description was read in six places and
written in zero, so the live recall index that wiki_section injects every
turn was titles-only — the page selector was running on the weakest input
it could have.

Stage 2 already holds the whole wiki and already picks a target page, so
asking for a one-line description on a NEW title costs almost nothing.

An existing page's description is filled if blank, never overwritten: the
first compiler to name a page wins."
```

---

## Task 11: Connection pool

§3.2 (🔴 "the worst area"), §17.8, O2. The codebase opens a connection per *operation*. Worst case `shared/agent_runs.py:append_step` — a 20-step turn opens and closes 20 connections. Long turns (Phase 5) make it materially worse.

`psycopg_pool.ConnectionPool` is the whole fix, behind the existing `team_session` / `user_session` signatures. No caller changes.

**Files:**
- Modify: `pyproject.toml` (add `psycopg-pool`)
- Modify: `shared/db.py`
- Create: `tests/test_db_pool.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `shared.db.close_pools()` for test teardown and clean shutdown. `team_session` and `user_session` keep their exact signatures.

- [ ] **Step 1: Add the dependency**

Run: `uv add psycopg-pool`
Expected: `pyproject.toml` gains `psycopg-pool>=3.2` and `uv.lock` updates.

- [ ] **Step 2: Write the failing test**

Create `tests/test_db_pool.py`:

```python
"""Connections come from a pool, not one per operation (findings §3.2).

The rule is "never open a connection per request"; the codebase opened one
per operation. append_step was the worst case — 20 connections for a 20-step
turn. This test pins the reuse rather than the implementation.
"""
from shared.db import Role, team_session, user_session
from tests._seed import A1, TEAM_A


def _backend_pid(conn):
    return conn.execute("select pg_backend_pid()").fetchone()[0]


def test_worker_sessions_reuse_a_backend(seeded):
    with team_session(Role.AGENT, TEAM_A) as conn:
        first = _backend_pid(conn)
    with team_session(Role.AGENT, TEAM_A) as conn:
        second = _backend_pid(conn)
    assert first == second


def test_user_sessions_reuse_a_backend(seeded):
    with user_session(A1) as conn:
        first = _backend_pid(conn)
    with user_session(A1) as conn:
        second = _backend_pid(conn)
    assert first == second


def test_a_returned_connection_carries_no_team_scope(seeded):
    """SET LOCAL dies with the transaction; a pooled reuse must not inherit it."""
    with team_session(Role.AGENT, TEAM_A) as conn:
        pass
    with team_session(Role.AGENT, TEAM_A) as conn:
        scoped = conn.execute(
            "select current_setting('app.current_team_id', true)"
        ).fetchone()[0]
    assert scoped == TEAM_A


def test_a_returned_user_connection_has_dropped_the_role(seeded):
    """SET LOCAL ROLE must not leak to the next borrower."""
    with user_session(A1) as conn:
        pass
    with user_session(A1) as conn:
        role = conn.execute("select current_user").fetchone()[0]
    assert role == "authenticated"
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/test_db_pool.py -v`
Expected: the two `reuse_a_backend` tests FAIL — each `psycopg.connect` gets a fresh backend pid.

- [ ] **Step 4: Pool the connections**

In `shared/db.py`, replace the `connect` / `team_session` / `user_session` block:

```python
import atexit
import json
from contextlib import contextmanager
from enum import Enum
from typing import Iterator

import psycopg
from psycopg_pool import ConnectionPool

from .config import settings

# ... Role and _URLS unchanged ...

# One pool per role, opened lazily. findings §3.2: the rule is "never open a
# connection per request" and this codebase opened one per OPERATION — a
# 20-step turn cost 20 connect/close cycles in append_step alone. The pool
# sits behind the existing session helpers, so no caller changes.
#
# ponytail: fixed size, no per-role tuning. Size from measurement if a role
# starts queueing.
_POOL_MIN, _POOL_MAX = 1, 10
_pools: dict[str, ConnectionPool] = {}


def _pool(url: str) -> ConnectionPool:
    pool = _pools.get(url)
    if pool is None:
        pool = ConnectionPool(url, min_size=_POOL_MIN, max_size=_POOL_MAX, open=True)
        _pools[url] = pool
    return pool


def close_pools() -> None:
    """Close every pool. For clean shutdown and test teardown."""
    for pool in _pools.values():
        pool.close()
    _pools.clear()


atexit.register(close_pools)


@contextmanager
def connect(role: Role) -> Iterator[psycopg.Connection]:
    """Borrow a connection as the given role. Caller manages transactions."""
    with _pool(_URLS[role]).connection() as conn:
        yield conn


@contextmanager
def team_session(role: Role, team_id: str) -> Iterator[psycopg.Connection]:
    """Borrow a worker connection scoped to one team for one transaction.

    SET LOCAL app.current_team_id binds current_team() in the RLS policies, so
    every read/write inside the block is confined to `team_id`. The
    transaction commits on clean exit and rolls back on error; SET LOCAL dies
    with it, so a pooled connection carries no scope to its next borrower.
    """
    if role is Role.ADMIN:
        raise ValueError("team_session is for worker roles, not ADMIN")
    with _pool(_URLS[role]).connection() as conn:
        with conn.transaction():
            conn.execute(
                "select set_config('app.current_team_id', %s, true)", (str(team_id),)
            )
            conn.execute(
                "select set_config('app.actor_kind', %s, true)", (_ACTOR_KIND[role],)
            )
            yield conn


@contextmanager
def user_session(user_id: str) -> Iterator[psycopg.Connection]:
    """Borrow a connection acting as an end user (role `authenticated`,
    auth.uid() = user_id), so RLS applies exactly as it would in the app.

    Commits on clean exit, rolls back on error. Connects as the dedicated
    authenticator role (comrade_authenticator: LOGIN + noinherit, may only
    SET ROLE authenticated) when COMRADE_AUTHENTICATOR_DB_URL is set; falls
    back to the admin URL for dev environments that predate the role.

    SET LOCAL ROLE and the jwt claims both die with the transaction, so a
    pooled connection never hands the next borrower someone else's identity.
    """
    url = settings.comrade_authenticator_db_url or _URLS[Role.ADMIN]
    with _pool(url).connection() as conn:
        with conn.transaction():
            conn.execute("set local role authenticated")
            conn.execute(
                "select set_config('request.jwt.claims', %s, true)",
                (json.dumps({"sub": str(user_id), "role": "authenticated"}),),
            )
            yield conn
```

⚠️ **The safety property this task must not break:** every scoping statement uses `SET LOCAL` / `set_config(..., true)`, so it is transaction-scoped and cannot survive into the next borrower. `psycopg_pool` also resets a connection on return. Tests 3 and 4 above are the guard — if either fails, stop: a leaked `SET ROLE` would be a cross-tenant identity bug.

- [ ] **Step 5: Run the pool tests**

Run: `uv run pytest tests/test_db_pool.py -v`
Expected: all four PASS.

- [ ] **Step 6: Run the full suite, including RLS isolation**

Run: `uv run pytest tests/test_rls_isolation.py -v && uv run pytest`
Expected: all PASS. RLS isolation is the test that would catch a leaked scope.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock shared/db.py tests/test_db_pool.py
git commit -m "perf: pool connections per role

findings §3.2 ('the worst area') and §17.8. The codebase opened a connection
per operation — append_step cost 20 connect/close cycles on a 20-step turn.
psycopg_pool sits behind the existing team_session/user_session signatures,
so no caller changes.

Two tests pin the safety property: SET LOCAL scoping and SET LOCAL ROLE both
die with the transaction, so a pooled connection can never hand the next
borrower another tenant's scope or another member's identity."
```

---

## Task 12: Correct the stale records

§24.6 plus the doc bug introduced in §13.6. These are the records a new session reads first, which makes them the highest-leverage stale text in the repo.

**Files:**
- Modify: `README.md`
- Modify: `HANDOFF.md`
- Modify: `docs/architecture.md`
- Modify: `docs/agent-architecture-findings-2026-08-12.md` (append a correction section)
- Modify: `C:\Users\ricky\.claude\projects\D--OneDrive-Desktop-Comrade\memory\project_comrade.md` and `MEMORY.md`

- [ ] **Step 1: Fix `README.md`**

- Line 3: replace *"An AI companion for student group projects."* with exactly:

  > An AI teammate for engineering teams that run without a manager. It reads the team's documents, chat and repository, compiles what the team has decided into a cited wiki, and acts only with a member's consent.

- Replace *"The agent never performs a group-visible write."* (false as written, §13.6) with exactly:

  > The agent never performs a group-visible action it chose itself.

- Delete any description of the T3 two-key flow and of `team_propose_group_message`.

- [ ] **Step 2: Fix `HANDOFF.md`**

- §3 API surface: delete `POST /consent/{id}/second_key` and the group-post proposal.
- §7 governance rulings (lines ~156–159): replace the T3 two-key ruling with a note that it was removed 2026-08-12 (§10), and that for code, GitHub branch protection is the stronger second key (§16.2) — while recording that the **non-code case now has no forced second pair of eyes at all** (§24.1).
- Gap 0b (line ~124): mark resolved.

- [ ] **Step 3: Fix `docs/architecture.md`**

- §5 consent: it names `post_group_message` as one of two executors. There is now one, `task_create`.
- Confirm the line *"no `INSERT` on `tasks` and no `UPDATE` on `messages`"* is still accurate — §13.6 says it is precise and correct; verify after Task 8's grant changes and extend it with the reads the agent no longer has.

- [ ] **Step 4: Append the correction to the findings doc**

Per §24.5-2, run `grep -n '^## ' docs/agent-architecture-findings-2026-08-12.md` first to pick a free section number. Append:

```markdown
## 26. Phase 0 executed — corrections to this document

> Appended 2026-08-29 after executing Phase 0 of
> `docs/superpowers/plans/2026-08-29-comrade-v2.md`.

### 26.1 🔴 New finding: the agent's group reply was blocked by RLS

`ag_messages_insert` (`20260612120000_action_consent.sql:33`) carried
`with check (... and thread_type = 'private' and sender_kind = 'ai')`, but
`server/app.py:_persist_ai_reply` inserts `thread_type='group'` on a group
turn under `Role.AGENT`. The reply to an `@comrade` invocation — one of the
two group-visible AI writes §13.1 explicitly keeps — therefore failed in
production.

Nothing caught it: `tests/test_server.py` and `tests/test_server_stream.py`
both monkeypatch `_persist_ai_reply`, and `tests/test_runtime_live.py` calls
`run_turn` directly rather than the endpoint.

**Corrects §13.6.** That section states `_persist_ai_reply` inserts group
messages "ungated". True at the GRANT level, false at the POLICY level. The
policy now checks `sender_kind = 'ai'` only — the invariant it exists to
protect is that the agent never writes a message attributed to a human.

### 26.2 The roadmap now lives in one place

§24.5-1 recommended consolidating §19 and §21. Done, in
`docs/superpowers/plans/2026-08-29-comrade-v2.md`. §8, §11, §19 and §21 are
superseded as sequencing authority and retained for their reasoning.

### 26.3 Decisions settled

- **Q1 (§10's open question):** `tier` survives, narrowed to `('T0','T1','T2')`.
- **Q5 (§7):** ADK session granularity is one per `(team, thread_type, thread_owner)` — the boundary is the person, not the channel (§4.2). Lands in Phase 1.
- **Q6 (§7):** a queued member sees the honest line, not a spinner.
- **Q8 (§13.4):** the private-thread publisher targets the current team only in v2.
- **Q9 (§23.5):** no metered overage in v2.
```

- [ ] **Step 5: Update project memory**

Rewrite `C:\Users\ricky\.claude\projects\D--OneDrive-Desktop-Comrade\memory\project_comrade.md` so the target reads "developer and engineering teams primary; any small team; students served by the free tier" (§14), and add a `MEMORY.md` pointer to the v2 plan.

- [ ] **Step 6: Verify no stale reference survives**

Run:

```bash
grep -rniE "second[_ ]key|countersign|two-key|T3|post_group_message|student group project" README.md HANDOFF.md docs/architecture.md
```

Expected: hits only inside historical/`docs/agent-architecture-findings-*.md` context or explicitly-marked "removed 2026-08-12" notes.

- [ ] **Step 7: Commit**

```bash
# docs/ is gitignored (kept on disk only) - stage the tracked files only
git add README.md HANDOFF.md
git commit -m "docs: correct the stale records after Phase 0

findings §24.6 plus the §13.6 doc bug. These are the records a new session
reads first.

- README no longer opens 'an AI companion for student group projects' (§14)
- 'the agent never performs a group-visible write' -> '...no group-visible
  action it chose itself'
- HANDOFF §7 records the T3 ruling as removed, and records that the non-code
  case now has no forced second pair of eyes (§24.1)
- architecture.md: one consent executor, not two
- findings doc gains §26 with the new RLS finding and the settled decisions"
```

---

## Phase 0 exit criteria

- [ ] `uv run pytest` green
- [ ] `uv run pytest -m live` green
- [ ] `cd frontend && npm run build && npm test && npm run test:integration && npm run test:e2e` green
- [ ] `grep -rn "post_group_message\|second_key\|add_second_key\|page_index" --include=*.py --include=*.ts --include=*.tsx --include=*.sql .` returns zero hits
- [ ] A manual `@comrade` in the group room against a local stack persists an AI reply (the Task 1 bug, end to end)
- [ ] `graphify . --update` re-run so the code graph reflects the deletions
- [ ] Branch merged to `master`

**Net effect of Phase 0:** roughly 400 lines deleted, ~80 added, six migrations, one live bug fixed, one latent 🔴 closed, and the two highest-measured-value memory corrections landed. No new capability.
