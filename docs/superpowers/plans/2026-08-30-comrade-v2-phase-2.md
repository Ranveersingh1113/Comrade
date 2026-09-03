# Comrade v2 — Phase 2: Perception

> Parent plan: [`2026-08-29-comrade-v2.md`](2026-08-29-comrade-v2.md). Ledgers: [Phase 0](2026-08-29-phase-0-ledger.md), [Phase 1](2026-08-30-phase-1-ledger.md).

**Goal:** Let the agent see the room it is sitting in. Today it answers questions about a conversation it cannot read, documents it cannot open, and deadlines it has no clock to judge. Five tools become eleven.

**Architecture:** Every new read tool runs as the requesting member (Phase 0's F1), so RLS scopes it for free — a private-thread search returns that member's own thread and nothing else. Every new tool is declared in `agent/registry.py` at birth (Phase 1's F8), so the chokepoint governs it from its first call. **This phase is the payoff for both.**

**Tech Stack:** unchanged. Postgres full-text (`tsvector` + `ts_rank_cd`) — no new dependency, runs inside RLS and inside the existing transaction.

---

## Global Constraints

Inherited. The five that bind this phase:

1. **RLS is the authorization layer.** New read tools use `user_session(requester_id)`. Explicit `team_id` filters are scoping, not authorization — under `authenticated` there is no `current_team()` and a member may belong to several teams.
2. **Every new tool gets a `REGISTRY` entry** in the same commit that adds it. An undeclared tool is refused by the chokepoint, so an unregistered tool is a broken tool.
3. **Every new gated action needs an executor AND a precheck** in `shared/consent.py`. `propose_action` now validates `tool_name` against `_EXECUTORS` at propose time, so a missing executor fails loudly at propose rather than at approve.
4. **Migration policy:** index on an existing table → `CREATE INDEX CONCURRENTLY` in its own file; check constraint on an existing table → `NOT VALID` then `VALIDATE`.
5. **Supabase grants `anon` and `authenticated` full CRUD on every new table by default.** Any new table needs an explicit `revoke` — see `docs/architecture.md`.

---

## The unlock this phase depends on

§2.1 of the findings doc warned that the agent role held team-scoped — not thread-scoped — SELECT on `messages`, and that the leak would go **live the moment a message-reading tool existed**. That tool is Task 3 of this phase.

Phase 0 closed it by deleting the agent's read grants entirely. `messages_search` is therefore safe to build *because* reads now run as the requesting member: a member searching their private thread gets their own thread, and `au_messages_select` (`is_team_member(team_id) AND (thread_type='group' OR thread_owner_id = auth.uid())`) is what makes that true, not application code.

**Do not add a `Role.AGENT` read path back to make any of these tools easier.**

---

## Task order and why

| # | Task | Delivers | Rationale |
|---|---|---|---|
| 1 | Search schema | `documents.parsed_text`, `messages` FTS index | Tools in Task 3 cannot exist without it. Two migrations (one needs `CONCURRENTLY`). |
| 2 | Fix the chat sweep | §3.2's worst query | Independent, and it runs every 5 seconds — the longer it stays, the more it costs. |
| 3 | `messages_search` + `document_read` | The two tools that most change what the agent can do | Needs Task 1. |
| 4 | `now()`, `task_get`, `task_propose_update` | Clock + task amendment | Independent of 1–3; needs a new consent executor. |
| 5 | `member_activity` + recency | The `idle` nudge finally has a data source | Needs a view change. |
| 6 | `propose_batch` | Multi-step work stops making five cards | Needs Phase 1's `batch_id`. Last because it is the least load-bearing. |

---

## Task 1: Schema for search

**Files:**
- Create: `supabase/migrations/20260830130000_document_parsed_text.sql`
- Create: `supabase/migrations/20260830140000_messages_fts_index.sql` *(own file — `CONCURRENTLY`)*
- Modify: `pipeline/compiler.py` (`handle_document_job` persists the parsed text)
- Create: `tests/test_document_parsed_text.py`

`documents.parsed_text` does not exist: extracted text lives only in a transient job payload (§3.1), so once a document is compiled its text is gone and `document_read` has nothing to open.

- [ ] **Step 1: failing test** — enqueue and run a document job, then assert `documents.parsed_text` holds the parsed text and `status='ready'`.

- [ ] **Step 2: run it, confirm the column does not exist.**

- [ ] **Step 3: migration one** — `alter table public.documents add column if not exists parsed_text text;` with a comment explaining it is the source for `document_read` and that it is written by `comrade_pipeline` only (the pipeline already holds `update` on `documents`; confirm rather than assume).

- [ ] **Step 4: migration two**, its own file, no transaction:

```sql
-- The index that makes messages_search possible. GIN over a to_tsvector
-- expression rather than a stored column: no schema change to `messages`, no
-- trigger to keep in sync, and the expression is the same one the query uses.
--
-- CONCURRENTLY per the migration policy — `messages` is an existing table and
-- this is the first index this project has added to one.
create index concurrently if not exists idx_messages_fts
  on public.messages using gin (to_tsvector('english', body));
```

**Implementer note:** Supabase's migration runner wraps files in a transaction and `CONCURRENTLY` cannot run inside one. Phase 0 hit this and it worked anyway — if it does not here, apply via `psql` and record the workaround in the file header. Do not silently drop `CONCURRENTLY`.

- [ ] **Step 5: persist the text** in `handle_document_job`, in the same `team_session` that already sets `status='ready'` — one statement, not a second connection.

- [ ] **Step 6: run, commit.** Include a live compile (`-m live`) since the document path is LLM-driven.

---

## Task 2: Fix the chat sweep

§3.2 calls this "the worst query in the codebase". `pipeline/chat.py:sweep_chat_compiles` runs **every 5 seconds** from `worker.tick()` and does a cross-team scan of the whole `messages` table with a correlated subquery per row. No index serves it.

**Files:**
- Create: `supabase/migrations/20260830150000_chat_sweep_index.sql` *(own file — `CONCURRENTLY`)*
- Modify: `pipeline/chat.py`
- Modify: `tests/test_chat_memory.py`

- [ ] **Step 1: failing test** — assert the sweep still enqueues exactly when it should (threshold, watermark, dedupe) after the rewrite. The existing tests in `test_chat_memory.py` already cover the behaviour; your job is to keep them green while changing the query, so **read them first** and add one that pins the new shape (e.g. a team below threshold is not enqueued even when another team is above it).

- [ ] **Step 2: the index**

```sql
-- Serves the ambient chat sweep, which runs every 5s from worker.tick().
-- Partial: the sweep only ever looks at undeleted human messages in group
-- rooms, so the index carries only those rows and stays small.
create index concurrently if not exists idx_messages_group_human
  on public.messages (team_id, created_at)
  where thread_type = 'group' and sender_kind = 'user' and deleted_scope is null;
```

- [ ] **Step 3: rewrite the query.** Replace the correlated subquery with a CTE computing each team's watermark once — `memory_compilations` is small, `messages` is not:

```sql
with wm as (
  select team_id, max(chat_through) as through
    from public.memory_compilations
   where chat_through is not null and status = 'done'
   group by team_id
)
select m.team_id
  from public.messages m
  left join wm on wm.team_id = m.team_id
 where m.thread_type = 'group' and m.sender_kind = 'user'
   and m.deleted_scope is null
   and m.created_at > coalesce(wm.through, '-infinity'::timestamptz)
 group by m.team_id
having count(*) >= %s
```

Keep the comment explaining why this scan is control-plane (`Role.ADMIN`) — it crosses teams by design, like the job claim.

- [ ] **Step 4: prove the plan improved.** Run `explain (analyze, buffers)` on the old and new queries against the seeded DB and put both in your report. This is the one task in the phase whose whole point is a query plan; "tests pass" does not demonstrate it.

- [ ] **Step 5: run, commit.**

---

## Task 3: `messages_search` and `document_read`

§5: *"The agent cannot read chat at all. It answers about a room it cannot see."* These are the two tools that most change what it can do.

**Files:**
- Modify: `agent/tools.py`, `agent/agent.py` (tool list + instruction), `agent/registry.py`
- Create: `tests/test_read_tools.py`

**Interfaces:**
- `search_messages(team_id, requester_id, query, limit) -> list[dict]` (pure)
- `read_document(team_id, requester_id, document_id) -> dict` (pure)
- ADK wrappers `messages_search(query, tool_context)` and `document_read(document_id, tool_context)` binding both ids from session state.

- [ ] **Step 1: the failing tests.** The isolation ones are the point:
  - a private message of A2's never appears in A1's search results, **and** prove it non-vacuously — A1 must be able to find their own private message with the same query;
  - a TEAM_B message never appears in a TEAM_A search, for a member of both;
  - a tombstoned (`deleted_scope` set) message is not returned;
  - `document_read` on another team's document returns a not-found shape, not a row;
  - `document_read` on a soft-deleted document (`deleted_at`) returns not-found.

- [ ] **Step 2: run, confirm failure.**

- [ ] **Step 3: implement.** Both read via `user_session(requester_id)` with an explicit `team_id` filter. Search ranks with `ts_rank_cd(to_tsvector('english', body), plainto_tsquery('english', %s))`, filters `deleted_scope is null`, caps at `limit`, and returns enough for the model to cite: sender display name, thread type, created_at, and the body. `document_read` returns `filename`, `kind`, `created_at`, `parsed_text`.

**Cap the payload.** A search that returns 50 full message bodies floods the context. Pick a sensible default limit and a per-body truncation, and say what you picked in the report. `document_read` on a large document needs the same treatment — a 200-page PDF's `parsed_text` must not arrive whole.

- [ ] **Step 4: register both tools** in `agent/registry.py` as `("db", writes=False, needs_human=False)` — reads that RLS already gates.

- [ ] **Step 5: teach the agent when to use them** in `agent/agent.py`'s instruction. Voice: concise, factual. Something like *"To answer about something said in the room, search it — don't guess. Cite who said it and when."*

- [ ] **Step 6: run, including a live test** where the agent answers a question only findable by searching chat.

---

## Task 4: `now()`, `task_get`, `task_propose_update`

§5: the agent *"can only create tasks — never amend, reassign, or close"*, and it has *"no clock; every deadline judgment is a guess."*

**Files:**
- Modify: `agent/tools.py`, `agent/agent.py`, `agent/registry.py`, `shared/consent.py`
- Create: `tests/test_task_tools.py`

- [ ] **Step 1: failing tests**, including the constraint below.

- [ ] **Step 2–4: implement.**
  - `now()` — returns the current UTC time as ISO-8601. One function. Register as `("db", writes=False, needs_human=False)`. It is not a DB call at all, but `db` is the honest surface for "reads server state"; do not invent a fourth surface for one tool.
  - `task_get(task_id)` — reads as the requester.
  - `task_propose_update(task_id, ...)` — proposes a `task_update` action into the consent queue. Needs a `_precheck_task_update` and `_exec_task_update` in `shared/consent.py`, and a `_TOOL_TIER_FLOORS` entry of `T1` (it affects one member — the assignee).

**🔴 The constraint that shapes this tool.** `trg_tasks_confirm_guard` says *only the assignee may confirm their own task*, and it checks `auth.uid()`. The executor runs as `comrade_executor`, where `auth.uid()` is **null** — so any executor-driven change that moves a task out of `proposed`, or sets `confirmed_at`, raises. That is correct and must not be worked around: an AI-proposed update must never confirm a task on a human's behalf.

Therefore `task_propose_update` may amend **`title`, `description`, `deadline`, `assignee_id`** and must **not** touch `status` or `confirmed_at`. Reassignment is allowed and the trigger will correctly void any prior confirmation. Write a test asserting a proposed status change is rejected rather than silently dropped.

- [ ] **Step 5: run, commit.**

---

## Task 5: `member_activity`, and a source for the `idle` nudge

§3.1 records that `contribution_v` has no recency signal and that the `idle` nudge type *"exists with no data source to trigger it"* — the machinery is built and nothing can fire it.

**Files:**
- Create: `supabase/migrations/20260830160000_contribution_recency.sql`
- Modify: `agent/tools.py`, `agent/agent.py`, `agent/registry.py`
- Create: `tests/test_member_activity.py`

- [ ] **Step 1: failing test** — `member_activity` returns, per active member, their last group message time, last task activity, and a `days_since_last_signal`.

- [ ] **Step 2–4:** add `last_message_at` and `last_task_at` to `contribution_v` as scalar subqueries in the existing style. §3.2 notes the view's message-count subquery already filters on `sender_id` with no covering index; add one if `explain` shows it matters, and say so either way.

Then `member_activity(team_id, requester_id)` reads the view as the requester. Register `("db", writes=False, needs_human=False)`.

**Governance note:** the platform-findings governance memo restricts person-to-person comparison. This tool exists to answer *"who might be stuck"*, not *"who is doing least"*. Return recency facts, not rankings, and do not add a sort by contribution volume.

- [ ] **Step 5: run, commit.**

---

## Task 6: `propose_batch`

§5: multi-step work produces five separate consent cards today. Phase 1 added `consent_queue.batch_id` for exactly this.

**Files:**
- Modify: `shared/consent.py`, `agent/tools.py`, `agent/registry.py`
- Modify: `frontend/src/screens/ConsentInbox.tsx`, `frontend/src/lib/types.ts`
- Create: `tests/test_propose_batch.py`

- [ ] **Step 1: failing test** — proposing three actions in one batch writes three rows sharing one `batch_id`, each independently approvable.

- [ ] **Step 2–4:** `propose_batch` stamps one `batch_id` across the proposals it writes. **Each item stays individually approvable and individually rejectable** — a batch is a display grouping, not an all-or-nothing gate. Approving four of five must work.

The frontend groups a batch under one heading with a per-item control, and shows progress ("2 of 5 approved").

- [ ] **Step 5: run backend + frontend, commit.**

---

## Phase 2 exit criteria

- [ ] `uv run pytest` green; `uv run pytest -m live` green
- [ ] Frontend build, unit (`--no-file-parallelism`), integration, e2e green
- [ ] `supabase db reset` applies every migration from scratch; roles re-applied; suite green
- [ ] **A private message of another member's is unreachable through `messages_search`**, proven non-vacuously
- [ ] The chat sweep's `explain` plan uses the new index, with before/after in the ledger
- [ ] A live turn answers a question that is only findable by searching chat
- [ ] Every new tool has a `REGISTRY` entry; `test_every_registered_tool_is_declared` still passes
- [ ] `graphify` refreshed; branch merged to `master`
