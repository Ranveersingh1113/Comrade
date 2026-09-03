# Comrade v2 — Phase 1: The Spine

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans`. Steps use checkbox (`- [ ]`) syntax.
>
> Parent plan: [`2026-08-29-comrade-v2.md`](2026-08-29-comrade-v2.md). Phase 0 ledger: [`2026-08-29-phase-0-ledger.md`](2026-08-29-phase-0-ledger.md). Reasoning source: `docs/agent-architecture-findings-2026-08-12.md`.

**Goal:** Give the agent a mandatory permission chokepoint, a memory of the conversation it is in, and the ability to learn from a rejection — the three things that turn a stateless tool-caller into something that behaves like a teammate.

**Architecture:** Phase 0 deleted; Phase 1 wires. Every mechanism here already ships in the installed `google-adk` 2.2.0 and is simply unused — except conversation memory, which is deliberately *not* taken from ADK (see the revision below).

**Tech Stack:** unchanged. Python 3.12 · FastAPI · google-adk 2.2.0 · psycopg3 + psycopg_pool · Postgres/Supabase RLS · React 19

---

## Global Constraints

Inherited from the parent plan. The five that bind this phase:

1. **RLS is the authorization layer.** Never re-implement or weaken it in application code.
2. **Migration policy:** index on an existing table → `CREATE INDEX CONCURRENTLY` outside a transaction; check constraint on an existing table → `NOT VALID` then `VALIDATE CONSTRAINT`.
3. **Unknown tool → `outbound`, `writes=true`, `needs_human=true`** (§15.4). Fails closed.
4. **The chokepoint governs tools that leave the sandbox; it does not govern the shell inside it** (§15.5). Gate `surface=outbound` and `surface=db`.
5. **Reads borrow the requester; writes act as the app** (§4.1). Landed in Phase 0 — do not regress it.

---

## Pre-flight: what I verified in the installed ADK before writing this

Run against `google-adk` 2.2.0 in `.venv`, 2026-08-30. This matters because §1 of the findings doc asserts these ship unwired, and one of them does not ship usably.

| Mechanism | Status | Consequence |
|---|---|---|
| `RunConfig.max_llm_calls` | ✅ present | Turn cap is free. Take it. |
| `BasePlugin` — 12 hooks incl. `before_tool_callback`, `on_tool_error_callback` | ✅ present | The chokepoint (F8) is fully supported today. |
| `App(name, root_agent, plugins, events_compaction_config, context_cache_config, resumability_config)` | ✅ present | `Runner(app=…)` accepts it; `run_async(…, run_config=…)` accepts the config. |
| `DatabaseSessionService` | ⚠️ **imports, but `ModuleNotFoundError: No module named 'sqlalchemy'`** | **Not usable without adding SQLAlchemy.** |

### 🔴 Revision to the parent plan's F11

The parent plan says "ADK session storage + `RunConfig`". Two things make ADK's session store the wrong tool here, and they compound:

1. **It needs SQLAlchemy**, a second data-access stack in a codebase that deliberately uses raw psycopg3 and hand-written SQL migrations.
2. **§3.1 already warned about the bigger problem:** `DatabaseSessionService` creates `sessions` / `events` / `app_states` / `user_states` **unqualified** — they land in `public` beside the RLS tables, with no RLS and no `team_id`. Phase 0 spent its entire budget proving that a table holding member content under a coarse policy is how private threads leak. Adding four such tables immediately afterwards would undo the phase.

**F11 is therefore split:**

- **F11a — the turn cap.** `RunConfig(max_llm_calls=…)`. Free, no dependency. Task 6.
- **F11b — conversation memory, sourced from `messages`.** `agent/runtime.py`'s own docstring already states the intent: *"Conversation history is the messages table's job (source of truth)."* That table already holds every turn, already has the right RLS, and — since Phase 0's F1 — is already read as the requesting member, so a private-thread turn sees exactly that member's thread and a group turn sees the room. Replaying the last N turns into the ADK request is ~20 lines, no new dependency, no new schema, and no new RLS surface. Task 6.

**What this gives up, stated plainly:** ADK's compaction (`events_compaction_config`), resumability, and prompt caching all key off its own session store. Revisit when a turn's history genuinely outgrows the window — the trigger is a measured context-length failure, not a preference. `App(...)` is still adopted in Task 2 for the plugin, so the door stays open.

---

## Task order and why

| # | Task | Delivers | Rationale for position |
|---|---|---|---|
| 1 | Cheap columns + fail-closed authenticator | F9, carried minor | One migration everything downstream reads. The `user_session` fallback is a 3-line fail-closed fix that should not wait behind a feature. |
| 2 | Tool registry + chokepoint | F8, G1, G2, G7 | The keystone. Phase 2 adds eight tools; the registry must exist first or each is a retrofit. |
| 3 | Consent column guard | carried Important | Needs Task 2's propose-time validation to be meaningful, and closes the "requester rewinds status" hole Phase 0 parked. |
| 4 | Rejections reach the model | F10, G3 | Needs Task 1's `resolution_reason`. Highest product value per line in the whole document. |
| 5 | `agent_steps` + `parent_run_id` | F13 | Fix the recorder's table before Task 6 restructures the recorder. |
| 6 | Conversation memory + turn cap | F11a, F11b | The loudest usability defect. After Task 5 so it touches a settled `runtime.py`. |
| 7 | Room advisory lock | F12 | Same file as Task 6; lands last so it wraps the final shape. |

---

## Task 1: Cheap columns, and make `user_session` fail closed

**Files:**
- Create: `supabase/migrations/20260830100000_agent_run_and_consent_columns.sql`
- Modify: `shared/db.py` (`user_session`)
- Modify: `tests/test_migration_production_hardening.py` *(or create `tests/test_migration_phase1_columns.py` — follow whichever pattern the existing migration tests use)*
- Create: `tests/test_user_session_failclosed.py`

**Interfaces produced:** `agent_runs.{input_tokens, output_tokens, cost_usd, parent_run_id}`; `agent_runs.status` accepts `'cancelled'`; `agent_runs.trigger_type` accepts `'agent'`; `consent_queue.{batch_id, agent_run_id, resolution_reason}`. Tasks 4 and 5 depend on these exact names.

- [ ] **Step 1: Write the failing tests**

`tests/test_user_session_failclosed.py`:

```python
"""user_session must never silently fall back to a BYPASSRLS connection.

shared/db.py falls back to the ADMIN url when COMRADE_AUTHENTICATOR_DB_URL is
unset. ADMIN is the table owner and bypasses RLS, so a production deployment
that forgets the variable silently runs every member query with no row
security at all — and, since Phase 0, shares a pool with it.

Failing closed turns a silent security downgrade into a startup error.
"""
import pytest

from shared import db


def test_user_session_refuses_to_fall_back_to_admin(monkeypatch):
    monkeypatch.setattr(db.settings, "comrade_authenticator_db_url", "")
    with pytest.raises(RuntimeError, match="COMRADE_AUTHENTICATOR_DB_URL"):
        with db.user_session("00000000-0000-0000-0000-000000000001"):
            pass
```

For the migration, add a test asserting each new column exists with the right type, and that `agent_runs.status` accepts `'cancelled'` and `trigger_type` accepts `'agent'`. Match the style of the existing `tests/test_migration_*.py` files — read one first.

- [ ] **Step 2: Run them and confirm they fail**

Run: `uv run pytest tests/test_user_session_failclosed.py -v`
Expected: FAIL — no `RuntimeError`; the fallback silently succeeds.

- [ ] **Step 3: Write the migration**

```sql
-- Cheap columns that unblock four separate later items. One migration,
-- because they are all `alter table ... add column` on two tables and
-- splitting them would mean four migrations for the same lock.
--
-- findings 3.1 listed these as missing; 23.4-3 promoted the token/cost pair
-- from housekeeping to the billing input; 25.7 asked for parent_run_id to be
-- taken "during the agent_steps migration" because it is near-zero cost
-- inside a migration that is happening anyway and painful to retrofit.
-- It is taken HERE instead, one task earlier, so agent_steps (Task 5) can
-- reference it from the start.

alter table public.agent_runs
  add column if not exists input_tokens  integer,
  add column if not exists output_tokens integer,
  add column if not exists cost_usd      numeric(12, 6),
  add column if not exists parent_run_id uuid references public.agent_runs(id) on delete set null;

create index if not exists idx_agent_runs_parent
  on public.agent_runs(parent_run_id) where parent_run_id is not null;

-- 'cancelled' completes the terminal set; 'agent' is the trigger type a
-- subagent run will carry (25.7 keeps the multi-agent door open without
-- walking through it). Both are widenings of an existing check constraint,
-- so per the migration policy: NOT VALID, then VALIDATE.
alter table public.agent_runs drop constraint if exists agent_runs_status_check;
alter table public.agent_runs add constraint agent_runs_status_check
  check (status in ('running','done','failed','cancelled')) not valid;
alter table public.agent_runs validate constraint agent_runs_status_check;

alter table public.agent_runs drop constraint if exists agent_runs_trigger_type_check;
alter table public.agent_runs add constraint agent_runs_trigger_type_check
  check (trigger_type in ('user','document','scheduled','agent')) not valid;
alter table public.agent_runs validate constraint agent_runs_trigger_type_check;

alter table public.consent_queue
  -- batch_id: the "Agent Inbox" batched approval the governance doc calls for
  -- (findings 5, propose_batch). Nullable; a lone proposal has no batch.
  add column if not exists batch_id uuid,
  -- agent_run_id: traces a proposal back to the turn that produced it (3.1).
  add column if not exists agent_run_id uuid references public.agent_runs(id) on delete set null,
  -- resolution_reason: the WHY a rejection carries back to the model (G3).
  -- Without it a rejection is a status change the agent can never learn from.
  add column if not exists resolution_reason text;

create index if not exists idx_consent_queue_batch
  on public.consent_queue(batch_id) where batch_id is not null;
```

**Implementer note:** confirm the real constraint names first —
`select conname from pg_constraint where conrelid='public.agent_runs'::regclass and contype='c';`
Postgres auto-names them and the names above are expected, not guaranteed.

Both `create index` statements are on **existing** tables. Per the migration policy they need `CONCURRENTLY`, which cannot run inside a transaction. If `supabase migration up` rejects it, split them into their own migration file and record the workaround in the header — do not silently drop `CONCURRENTLY`.

- [ ] **Step 4: Make `user_session` fail closed**

In `shared/db.py`, replace the fallback:

```python
    url = settings.comrade_authenticator_db_url or _URLS[Role.ADMIN]
```

with:

```python
    # No fallback. ADMIN is the table owner and BYPASSRLS, so falling back to
    # it would run every member query with no row security — silently, and
    # since the pool landed, sharing a pool with the control plane too. A
    # missing variable must stop the process, not quietly disable RLS.
    url = settings.comrade_authenticator_db_url
    if not url:
        raise RuntimeError(
            "COMRADE_AUTHENTICATOR_DB_URL is not set. user_session() will not"
            " fall back to the admin connection, which bypasses RLS."
        )
```

Update the docstring's "falls back to the admin URL for dev environments that predate the role" sentence to say it now raises.

- [ ] **Step 5: Apply, run, commit**

Run: `supabase migration up && uv run pytest`
Expected: all green.

```bash
git add supabase/migrations/20260830100000_agent_run_and_consent_columns.sql shared/db.py tests/
git commit -m "feat: columns for cost, batching, tracing and rejection reasons

findings 3.1 and 23.4-3. One migration for four later items: token/cost on
agent_runs (the billing input), parent_run_id (25.7 -- keeps the multi-agent
door open), cancelled status and agent trigger type, plus batch_id,
agent_run_id and resolution_reason on consent_queue.

Also makes user_session fail closed. It fell back to the ADMIN url when
COMRADE_AUTHENTICATOR_DB_URL was unset; ADMIN is the table owner and bypasses
RLS, so a missing variable silently disabled row security for every member
query. Now it raises."
```

---

## Task 2: The tool registry and the mandatory chokepoint

§15.4, §15.5, and gaps G1/G2/G7. **The keystone of the phase.**

Today gating is per-tool convention: `team_propose_task` routes through `propose_action`; `member_send_nudge` writes directly. A sixth tool that forgets to route through `propose_action` is ungated and nothing catches it. There is no field for a gate to read (G7), and unknown means "whatever the body does" rather than "ask" (G2).

**Files:**
- Create: `agent/registry.py`
- Create: `agent/permission_plugin.py`
- Modify: `agent/agent.py` (build an `App`, attach the plugin)
- Modify: `agent/runtime.py` (`Runner(app=…)` instead of `InMemoryRunner(agent=…)`)
- Modify: `shared/consent.py` (validate `tool_name` at propose time)
- Create: `tests/test_tool_registry.py`, `tests/test_permission_plugin.py`

**Interfaces produced:**
- `agent.registry.Surface` — `"sandbox" | "db" | "outbound"`
- `agent.registry.ToolSpec(surface, writes, needs_human)`
- `agent.registry.REGISTRY: dict[str, ToolSpec]`
- `agent.registry.spec_for(tool_name) -> ToolSpec` — **returns the fail-closed default for an unregistered name, never raises**
- `agent.permission_plugin.ChokepointPlugin`

- [ ] **Step 1: Write the failing tests**

`tests/test_tool_registry.py`:

```python
"""Every tool declares its surface; an unclassified one fails closed.

findings 15.4. Claude Code's Tool.ts defaults isReadOnly -> false and
isConcurrencySafe -> false: forgetting to declare gets you the DANGEROUS
assumption. That inversion is the whole point — a registry whose default is
permissive is a registry that only protects the tools someone remembered.
"""
from agent.registry import REGISTRY, spec_for


def test_every_registered_tool_is_declared():
    from agent.agent import root_agent

    for tool in root_agent.tools:
        assert tool.__name__ in REGISTRY, f"{tool.__name__} is not declared"


def test_an_unknown_tool_fails_closed():
    spec = spec_for("some_tool_nobody_classified")
    assert spec.surface == "outbound"
    assert spec.writes is True
    assert spec.needs_human is True


def test_read_only_tools_are_declared_as_such():
    assert spec_for("team_get_state").writes is False
    assert spec_for("memory_read_page").writes is False


def test_the_nudge_is_declared_as_an_outbound_write():
    """member_send_nudge acts immediately and reaches another member's thread.

    findings 9 notes the asymmetry: after 13 the agent may not put a word in
    the shared room on its own initiative, yet may still DM a teammate who
    never asked. The registry must at least SAY so.
    """
    spec = spec_for("member_send_nudge")
    assert spec.surface == "outbound"
    assert spec.writes is True
```

`tests/test_permission_plugin.py` — the gate itself. Assert that:
1. a `writes=False, surface="db"` tool is allowed through untouched;
2. a tool absent from the registry is **blocked**, and the block reaches the model as a tool result explaining why (not an exception that kills the turn);
3. `surface="sandbox"` is allowed through without a human gate (§15.5 — there is no sandbox yet, so assert the policy on a synthetic spec rather than inventing a tool).

Write these against `ChokepointPlugin.before_tool_callback` directly, constructing the ADK callback context by hand. Read `.venv/Lib/site-packages/google/adk/plugins/base_plugin.py` for the exact signature before writing — do not guess it.

- [ ] **Step 2: Run and confirm they fail** (`ModuleNotFoundError: agent.registry`)

- [ ] **Step 3: Write `agent/registry.py`**

```python
"""What each tool is allowed to touch, and whether a human must see it first.

findings 15.4. Three columns, one row per tool:

  surface      which boundary the call crosses — sandbox | db | outbound
  writes       does it change anything (conservative default: True)
  needs_human  must a member approve before it happens (default: True for
               anything outbound)

The load-bearing property is the DEFAULT, not the table: an unregistered tool
resolves to outbound/writes/needs_human, so a tool nobody classified fails
closed. Claude Code's Tool.ts makes the same inversion — isReadOnly defaults
to False, "assume writes" — and it is why forgetting to declare is safe there.

Scope (15.5): the chokepoint governs `db` and `outbound`. It does NOT govern
a shell inside the sandbox — if the sandbox holds no credentials and cannot
reach anything unproxied, per-command approval buys nothing and costs the
capability the owner refused to trade away.
"""
from dataclasses import dataclass
from typing import Literal

Surface = Literal["sandbox", "db", "outbound"]


@dataclass(frozen=True)
class ToolSpec:
    surface: Surface
    writes: bool
    needs_human: bool


# The fail-closed default. Anything not in REGISTRY resolves to this.
UNKNOWN = ToolSpec(surface="outbound", writes=True, needs_human=True)

REGISTRY: dict[str, ToolSpec] = {
    # Reads. The agent runs these as the requesting member (findings 4.1), so
    # RLS is already the gate — nothing to add.
    "team_get_state":    ToolSpec("db", writes=False, needs_human=False),
    "memory_read_page":  ToolSpec("db", writes=False, needs_human=False),
    # Proposes into the consent queue. The write it describes is gated by the
    # queue itself, so the TOOL call is not the thing a human approves.
    "team_propose_task": ToolSpec("db", writes=True, needs_human=False),
    # Sends immediately into another member's private thread, with no consent
    # gate — the agent's one ungated write (findings 9). Declared outbound so
    # the asymmetry is visible in the table rather than only in a doc.
    "member_send_nudge": ToolSpec("outbound", writes=True, needs_human=False),
}


def spec_for(tool_name: str) -> ToolSpec:
    """The tool's declaration, or the fail-closed default. Never raises."""
    return REGISTRY.get(tool_name, UNKNOWN)
```

**Note on `needs_human=False` for the two writers:** both are already gated — `team_propose_task` writes only a proposal, and `member_send_nudge` is a deliberate exception recorded in §13.7. `needs_human=True` on a tool whose whole job is to *create* the approval request would deadlock. Say this in the code, not just here.

- [ ] **Step 4: Write `agent/permission_plugin.py`**

The plugin implements `before_tool_callback`. Behaviour:

- resolve `spec_for(tool.name)`;
- if the tool is **not in `REGISTRY`**, return a tool-result dict refusing the call and naming the tool — returning a value from `before_tool_callback` short-circuits the tool and hands your value back to the model, which is exactly the "unknown → refuse, and tell the model why" behaviour G2 asks for;
- if `spec.surface == "sandbox"`, return `None` (allow) — §15.5;
- otherwise return `None` (allow): `db` and `outbound` tools that ARE registered have their own gates.

Confirm the short-circuit semantics in `base_plugin.py` before relying on them; if returning a value does not short-circuit in 2.2.0, raise a `ValueError` the runtime converts into a tool error instead, and say so in the report.

- [ ] **Step 5: Wire the plugin through an `App`**

In `agent/agent.py`, replace the bare `root_agent` export with an `App` that carries it plus the plugin. In `agent/runtime.py`, replace `InMemoryRunner(agent=root_agent, app_name=_APP_NAME)` with `Runner(app=…, session_service=…)`. `InMemoryRunner` is the dev-mode helper (§1); moving off it is the point.

Keep `root_agent` exported — `tests/test_agent.py` and `evaluation/` import it.

- [ ] **Step 6: Validate `tool_name` at propose time**

In `shared/consent.py:propose_action`, before the insert:

```python
    if tool_name not in _EXECUTORS:
        raise ConsentError(
            f"no executor registered for {tool_name} — refusing to queue a"
            " proposal that could never execute"
        )
```

§13.5 is the justification: today a typo'd or removed tool produces a pending card that can never execute, and it fails only when a human approves it — the worst possible moment. This is also what would have turned Phase 0's `post_group_message` removal into a red suite instead of a green one.

- [ ] **Step 7: Handle the duplicate-proposal 500** *(carried from Phase 0's review)*

Phase 0 added `unique (team_id, action_hash) where status='pending'`. A retried turn now raises `UniqueViolation` out of `propose_action` and 500s the whole turn. Catch it and return the existing proposal instead — a duplicate proposal is not an error, it is the idempotency the constraint exists to provide:

```python
    except psycopg.errors.UniqueViolation:
        # The same proposal is already pending. That is the constraint doing
        # its job (findings 2.2), not a failure — hand back the existing one.
```

Add a test proving a duplicate returns the first proposal's id rather than raising.

- [ ] **Step 8: Run everything, commit**

`uv run pytest` and `uv run pytest -m live tests/test_runtime_live.py -v` — the live one matters because it is the only test that exercises a real ADK turn through the new `App` and plugin.

---

## Task 3: A consent row's requester may change only what a decision needs

**Carried Important finding from Phase 0's whole-branch review.** `au_consent_queue_update` restricts *rows* and nothing else, so a requester can `update` any column on their own pending row: rewind `status` to `'pending'` and re-approve to execute twice, or rewrite `team_id`, `action_hash`, `tool_args` or `tier`.

Exactly-once holds against retries, races and the agent. It does not hold against the requester. Pre-existing, bounded to their own item, and now the last hole in the consent invariant.

**Files:**
- Create: `supabase/migrations/20260830110000_consent_column_guard.sql`
- Create: `tests/test_consent_column_guard.py`

- [ ] **Step 1: Write the failing test** — as `A1`, approve an item, then attempt to set `status='pending'` again; expect a raise. Also attempt to rewrite `action_hash`, `team_id` and `tier` on a pending row; expect raises. Then prove the legitimate transitions still work: `pending → approved`, `pending → rejected`, and `edit_and_approve`'s `tool_args` + `action_hash` + `status='edited'` rewrite.

- [ ] **Step 2: Run, confirm every "attacker" case currently SUCCEEDS.** That is the finding.

- [ ] **Step 3: Write a `security definer` trigger** narrowing what a human actor may change, in the shape of the deleted `trg_consent_second_key_guard` — read `20260719130000_consent_tiers.sql` for the pattern, including how it lets worker roles (`auth.uid() is null`) through untouched. The rules:
  - a member may move `pending → approved | rejected | edited`, and nothing else;
  - `edited` may also rewrite `tool_args` and `action_hash` — that is what `edit_and_approve` is;
  - a member may never write `team_id`, `requesting_member_id`, `tier`, `reversible`, `expires_at`, `agent_run_id`, or move a resolved row back to `pending`;
  - `resolution_reason` is writable alongside a rejection (Task 4 needs this).

- [ ] **Step 4: Re-run; every attacker case now raises and every legitimate transition still passes. Run the full suite. Commit.**

---

## Task 4: A rejection reaches the model

§9.3 G3. *"Highest product value per line in this list — today the agent cannot learn inside a session."*

Right now `reject_consent` sets a status and returns. The turn ended long ago. The agent never learns it was rejected, or why, and will propose the same thing again.

**Files:**
- Modify: `shared/consent.py` (`reject_consent` takes a reason)
- Modify: `server/app.py` (the reject endpoint accepts one)
- Modify: `agent/agent.py` (a prompt section carrying recent rejections)
- Modify: `frontend/src/lib/agentApi.ts`, `frontend/src/components/ConsentCard.tsx` (an optional reason field on reject)
- Create: `tests/test_rejection_feedback.py`

- [ ] **Step 1: Write the failing test** — reject a proposal with the reason `"we already decided this in standup"`, then assert that the string appears in the instruction `build_instruction` produces for that team.

- [ ] **Step 2–4:** thread `reason: str | None` through `reject_consent` into `resolution_reason`; add a `recent_rejections(team_id, requester_id)` projection reading rejected items from the last N days **as the requesting member** (Phase 0's F1 — do not reach for the agent role); render it into the instruction as a short section; give the frontend an optional one-line reason input on the reject button.

**Voice note for the prompt section:** state it as fact, not scolding — *"These proposals were rejected recently, with the member's reason. Do not re-propose them unless something changed."* The agent's voice guide is warm and factual; a nagging block would read as the product arguing with its user.

- [ ] **Step 5: Full suite + frontend. Commit.**

---

## Task 5: `agent_steps`, and the end of the jsonb rewrite

§3.2. `shared/agent_runs.py:append_step` is the worst function in the codebase: `steps = steps || %s::jsonb` rewrites the entire array on every step, so an N-step turn writes O(N²) bytes, and `agent_runs` is named the highest-bloat table in the audit. Phase 0's pool fixed the connection half; this fixes the write half.

**Files:**
- Create: `supabase/migrations/20260830120000_agent_steps.sql`
- Modify: `shared/agent_runs.py`
- Modify: `agent/runtime.py` (if the step-append signature changes)
- Modify: `tests/test_agent_runs.py`

- [ ] **Step 1: Write the failing test** — append three steps, read them back in order, and assert `agent_runs.steps` is no longer the storage. Assert `seq` ordering is preserved and that appending to a nonexistent run still raises `LookupError`.

- [ ] **Step 2–4:** create `agent_steps (id, run_id, team_id, seq, type, tool, args jsonb, response jsonb, text, created_at)` with `unique (run_id, seq)` and an index on `(run_id, seq)`; grant `insert, select` to `comrade_agent` only — **members get no grant**, for exactly the reason Phase 0's `agent_runs` leak established: these rows hold tool arguments and results from private turns. Rewrite `append_step` to a plain insert and `get_run` to join. Keep `agent_runs.steps` for now but stop writing it; drop it in a later migration once nothing reads it.

- [ ] **Step 5: Full suite. Commit.**

---

## Task 6: The agent remembers the conversation, and turns are capped

**F11a + F11b. See the pre-flight revision above** — conversation history comes from `messages`, not from ADK's session store.

**Files:**
- Modify: `agent/runtime.py`
- Create: `agent/history.py`
- Modify: `shared/config.py` (`agent_max_llm_calls`, `agent_history_turns`)
- Create: `tests/test_agent_history.py`

- [ ] **Step 1: Write the failing test** — seed a private thread with two prior messages, run a turn, and assert the prior messages reach the model. Assert the group/private boundary: a private-thread turn must not pull the group room's messages into history, and vice versa. Assert the cap: history is bounded to `agent_history_turns`.

- [ ] **Step 2–4:** `agent/history.py` exposes `recent_turns(team_id, requester_id, thread_type, limit) -> list[types.Content]`, reading `messages` **as the requesting member** — RLS then does the scoping work for free, and a private turn provably cannot see another member's thread. Feed it into the ADK session at creation. Add `RunConfig(max_llm_calls=settings.agent_max_llm_calls)` to `run_async`.

**Do not** add SQLAlchemy. **Do not** create ADK's session tables in `public`.

- [ ] **Step 5:** the live test is the real proof here. `uv run pytest -m live` — and add one live case that asks a follow-up question depending on the previous message ("what did I just ask you?").

- [ ] **Step 6: Commit.**

---

## Task 7: One agent turn at a time per room

§4.3. Two simultaneous runs in one room means two agents that cannot see each other: duplicated work, contradictory answers, and a race on the consent queue. It stops reading as one teammate.

**Files:**
- Modify: `agent/runtime.py`
- Modify: `shared/db.py` (an advisory-lock helper)
- Create: `tests/test_room_lock.py`

- [ ] **Step 1: Write the failing test** — two concurrent group turns for one team must serialise; two concurrent *private* turns for different members must NOT (they share no surface, §4.3).

- [ ] **Step 2–4:** a Postgres advisory lock keyed on `team_id`, held for the turn. Group turns take it; private turns do not. **Decision Q6 applies:** a queued member sees the honest line — *"Comrade is on Maya's question, yours is next"* — not a spinner, so surface the wait to the caller rather than blocking silently.

- [ ] **Step 5: Full suite + live. Commit.**

---

## Phase 1 exit criteria

- [ ] `uv run pytest` green; `uv run pytest -m live` green
- [ ] `cd frontend && npm run build && npx vitest run --no-file-parallelism && npm run test:integration && npm run test:e2e` green *(serial: this machine's vitest workers die under Docker contention — Phase 0 ledger)*
- [ ] `supabase db reset` applies every migration from scratch, then the suite is green on the rebuilt DB
- [ ] A tool added without a `REGISTRY` entry is refused, and the refusal reaches the model
- [ ] A rejected proposal's reason is visible in the next turn's instruction
- [ ] The agent answers a follow-up question that depends on the previous message
- [ ] `graphify` refreshed; branch merged to `master`
