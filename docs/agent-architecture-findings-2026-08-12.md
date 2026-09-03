# Agent architecture & memory findings — 2026-08-12

> Session output: comparison of Comrade's agent loop against Claude Code, an audit of
> the DB against Postgres best practice, an assessment of the memory pipeline against
> two external architectures, and the design decisions reached.
>
> Supersedes nothing. Complements the 2026-07-21 production-readiness checkpoint —
> that list is about shipping what exists; this one is about what the agent layer
> still lacks.
>
> Verification status is marked per finding. Anything sourced from a third-party
> website or README is that project's own claim, not verified behaviour.

---

## 1. The architecture decision: keep ADK

**Question asked:** should Comrade's agent core be re-architected on Claude Code's
harness model?

**Answer: no.** The gaps are real but they are *unwired* gaps, not architecture gaps.

Comrade has `google-adk` **2.2.0** installed (`pyproject.toml:7`) and uses almost none
of it. `agent/runtime.py:72` calls `InMemoryRunner(agent=root_agent)` — the dev-mode
helper — with a fresh session per turn, no `App`, no plugins, no `RunConfig`.

Every "missing loop feature" already ships in the installed package (verified by
reading `.venv/Lib/site-packages/google/adk/`):

| Believed missing | Already available, unwired |
|---|---|
| Turn cap | `RunConfig.max_llm_calls` (defaults to 500) |
| Conversation memory | `sessions/database_session_service.py` |
| Permission gate (Claude Code's `canUseTool`) | `plugins/base_plugin.py` → `before_tool_callback` |
| Compaction | `App.events_compaction_config` (`apps/compaction.py`) |
| Cost tracking | `after_model_callback` → usage metadata |
| Tool-error retry | `plugins/reflect_retry_tool_plugin.py` |
| Resume | `App.resumability_config` |
| Prompt caching | `App.context_cache_config` |
| Mid-turn message injection | `before_model_callback` (receives mutable `llm_request`) |
| Skills | `skills/skill_registry.py` |

`BasePlugin` exposes 13 hooks including `before_tool_callback`, `on_tool_error_callback`,
`before_model_callback`, `after_model_callback`.

Porting Claude Code's `queryLoop` (1,730 lines of TypeScript) would mean rewriting all
of the above in Python to replace a library that already has it.

**What Claude Code genuinely has no answer for:** everything multiplayer. It is
single-player by design — one user, one working directory, one blocking permission
prompt. That layer is Comrade's to build either way, and choosing to port the loop
would mean building it on top of a loop you also had to write.

### What Claude Code *is* worth copying

Not the loop. These patterns:

1. **Tool contract discipline** — rich descriptions, validated input, declared
   concurrency safety (`Tool.ts`, `services/tools/toolOrchestration.ts`).
2. **Queued-input injection at the tool boundary** (`query.ts:1568`) — see §5.
3. **Progressive disclosure of context** — index always in prompt, bodies on demand.
   Comrade already copied this for the wiki (`agent/agent.py:62` says so).

---

## 2. Bugs found

### 2.1 🔴 Agent role can read every member's private thread

`supabase/migrations/20260612095500_rls.sql:248`:

```sql
create policy ag_messages on public.messages for all to comrade_agent
  using (team_id = public.current_team()) with check (team_id = public.current_team());
```

Team-scoped, **not thread-scoped**. `thread_owner_id` is never checked, and
`comrade_agent` holds `select` on `public.messages` (same file, line 224).

**Status:** latent. No tool currently exposes messages to the model, so it is not
exploitable today. It becomes live the moment `messages_search` (or any message-reading
tool) exists.

**Impact if live:** breaks the hardest product invariant — "private AI threads are
invisible to everyone." Member A asks a question; the agent answers using content from
member B's private thread.

**Fix:** see §4.1. The fix is to *delete* the agent's read policies, not add to them.

### 2.2 No idempotency on consent proposals

`consent_queue.action_hash` is computed (`shared/consent.py:48`) and stored, but has
**no unique constraint** — the only index on the table is
`idx_consent_queue_team_status (team_id, status)`.

A retried turn silently creates a duplicate proposal. Live now.

**Fix:** `unique (team_id, action_hash) where status = 'pending'`.

### 2.3 `memory_pages.description` is never written

`pipeline/compiler.py:225` (`_resolve_page`) inserts `(team_id, title)` only. Grep
confirms `description` is read in 6 places and written in **zero**.

Consequences:
- The agent's recall index (`agent/agent.py:73`) shows bare titles.
- The consolidator's page blocks show bare titles (`compiler.py:144`).
- `render_team_wiki` never shows descriptions.

The documented design — "LLM selector picks pages by description, Claude-Code style"
(`pipeline/wiki.py:7`) — is unrealized. The model chooses which page to open from the
title alone.

**Fix:** stage 2 already has the whole wiki in context; ask it for a one-line
description when it proposes a new page title.

### 2.4 `page_index()` is dead code

`pipeline/wiki.py:58`. Called only by `tests/test_wiki.py`. The agent uses
`all_active_pages` directly.

### 2.5 Pre-existing, from the 2026-07-21 checkpoint (still open)

`POST /observations/{id}/suppress` does not validate that the target message is
actually a memory diff card.

---

## 3. Database assessment

### 3.1 Against the schema needs of the proposed agent capabilities

| Missing | Blocks |
|---|---|
| Any session / conversation storage | Conversation memory. ADK's `DatabaseSessionService` creates `sessions` / `events` / `app_states` / `user_states` **unqualified** — they would land in `public` beside the RLS tables, with no RLS and no `team_id`. Needs its own schema, revoked from `authenticated`, plus a `(team, thread, owner) → session_id` map |
| `documents.parsed_text` | `document_read`. Extracted text currently lives only in a transient job payload (`compiler.py:389`) |
| Full-text index on `messages` | `messages_search` at useful quality |
| Token / cost columns on `agent_runs` | Any real budget. `_check_turn_budget` counts *turns* |
| `agent_runs.parent_run_id` | Subagents — the table is flat |
| `agent_runs`: `cancelled` status, `agent` trigger type | Cancellation; subagent runs |
| `consent_queue.batch_id` | The batched "Agent Inbox" approvals the governance doc already calls for |
| `consent_queue.agent_run_id` | Tracing a proposal back to the turn that produced it |
| Recency signal in `contribution_v` | The `idle` nudge type exists **with no data source to trigger it** |

Not urgent: memory search indexes. Embeddings were deliberately dropped 2026-07-15
(`20260715100000_drop_vector_retrieval.sql`) and the whole-wiki-in-context approach
holds at pilot scale.

### 3.2 Against Postgres best practice

Audited against `postgres_rules.md` (adapted from Hatchet's Postgres survival guide).

#### Schema — pass, with notes

- Primary key on every table. ✅
- `timestamptz` everywhere, zero naked `timestamp`. ✅
- **UUID v4 PKs** (`gen_random_uuid()`) — random, so poor btree locality → page splits
  and index bloat on high-insert tables (`messages`, `change_log`, `github_activity`).
  PG18's `uuidv7()` is sequential. Changing existing tables is expensive (FKs
  everywhere); use `uuidv7()` for *new* high-write tables only.
- **`agent_runs.steps jsonb` is jsonb misuse.** The shape is known (`seq`, `type`,
  `tool`, `args`). Should be an `agent_steps` table. Also blocks `parent_run_id` and
  queryable steps.
- **Cascades on high-volume tables** — `messages.team_id ... on delete cascade`. Team
  deletion takes locks across all messages and change_log. Rare, but a long lock at scale.

#### Read queries — three real gaps

**🔴 The chat sweep query is the worst in the codebase.** `pipeline/chat.py:130`, run
**every 5 seconds** from `worker.tick()`:

```sql
select m.team_id from public.messages m
 where m.thread_type='group' and m.sender_kind='user' and m.deleted_scope is null
   and m.created_at > coalesce((
     select max(c.chat_through) from public.memory_compilations c
      where c.team_id = m.team_id and c.chat_through is not null and c.status='done'),
     '-infinity'::timestamptz)
 group by m.team_id having count(*) >= %s
```

Cross-team scan of the whole `messages` table plus a correlated subquery per row. No
index serves it. Fine at 100 messages; dies at 100k.

**🟡 `idx_messages_private_owner` is missing its sort column.** Partial index on
`(thread_owner_id) where thread_type = 'private'`. The private-thread query orders by
`created_at`, so every read sorts. Rule: ORDER BY columns go last in a compound index.
Should be `(thread_owner_id, created_at)`.

**🟡 `contribution_v` has an unindexed filter.** Its message-count subquery filters on
`sender_id`; the available index is `(team_id, thread_type, created_at)`. Four scalar
subqueries × N members.

Passing: `idx_messages_team_thread (team_id, thread_type, created_at)` is textbook —
equality, equality, then ORDER BY. `idx_jobs_status` and `idx_consent_queue_team_status`
both align with their queries.

#### Write queries — pass on the one that matters

**No LLM call happens inside a transaction.** Verified on both compile paths
(`compiler.py:355`, `chat.py:168`): open transaction → read pages → **close** → LLM call
→ open transaction → write. Deliberate and correct.

#### 🔴 Connections — the worst area

The rule is "never open a connection per request." The codebase opens one per
*operation*.

Worst case, `shared/agent_runs.py:28`:

```python
def append_step(team_id, run_id, step):
    with team_session(Role.AGENT, team_id) as conn:   # NEW CONNECTION
        conn.execute("update public.agent_runs"
                     " set steps = steps || %s::jsonb, current_step = current_step + 1 ...")
```

Called once per step. A 20-step turn opens and closes **20 connections** and performs 20
full-jsonb rewrites (`steps || x` rewrites the whole array each time → O(N²) bytes
written). This single function violates the connection rule, the batching rule, and the
bloat rule simultaneously.

Other counts:
- One document compile = 3 connections (`compiler.py:355`, `:365`, plus the job update)
- `worker.tick()` = ≥3 connections every 5 seconds, even when idle
- Every HTTP request opens fresh connections

No pgbouncer, no in-process pool. Flagged in the platform audit since 2026-06-27.

#### Migrations — one past violation, risk ahead

Most `create index` statements run in the same migration that creates the (empty) table,
which is safe.

- ⚠️ `20260719090000_production_hardening.sql:10` creates `idx_jobs_lease_expiry` on the
  **existing** `jobs` table without `CONCURRENTLY`. Harmless at the time (small table),
  but it sets a precedent.
- ⚠️ `20260719130000_consent_tiers.sql` adds check-constrained columns to an existing
  table directly, rather than `NOT VALID` + `VALIDATE CONSTRAINT`.

**Policy going forward:** any index on an existing table uses `CREATE INDEX CONCURRENTLY`
(outside a transaction); any check constraint on an existing table uses `NOT VALID` then
`VALIDATE`.

#### ✅ Queues — textbook

`pipeline/worker.py:33` `_CLAIM_SQL` is exactly the recommended pattern:

```sql
update public.jobs set status='processing', attempts=attempts+1, ...
 where id = (select id from public.jobs
              where (status='pending' or (status='processing' and lease_expires_at < now()))
                and attempts < 3
              order by created_at for update skip locked limit 1)
 returning id, team_id, job_type, payload, attempts
```

Plus lease expiry, an attempts cap, and a dedupe key. Best-engineered part of the DB.
No change needed.

*(Note: the advisory lock proposed in §5 for room serialization is not a queue —
mutual exclusion is a valid use of advisory locks. The rule warns against using them
*as* a queue.)*

#### Autovacuum — untuned

| Table | Churn | Risk |
|---|---|---|
| `agent_runs` | jsonb append per step → full-row rewrite | **Highest bloat** |
| `jobs` | 2–3 updates per row (claim → finish) | High |
| `messages` | insert-only | Low |
| `change_log` | insert-only, unbounded growth | Eventual partition candidate |

The `agent_runs` bloat disappears entirely once steps move to their own table.

#### Partitioning — not yet

`messages` and `change_log` are the eventual candidates. Neither is large.

---

## 4. Design decisions reached

### 4.1 The agent borrows the requester's permissions

Not its own. This is the fix for §2.1 and the single most important decision in this
session.

**Reads** run as the requesting member via the *already existing* `user_session()`
(`shared/db.py:74`) — it sets `request.jwt.claims` and `SET ROLE authenticated`, so
queries run under that member's own RLS. **Writes** stay on `Role.AGENT` (propose-only
into the consent queue).

This means **deleting** the `ag_*` read grants rather than adding new policies.

Side effect: `team_get_state` would then return only the caller's own pending consent
items, because that is what `au_consent_queue` allows. That is more correct than the
current behaviour.

Independently validated by PromptQL's "user identity, not agent identity" model
(§6.1) — the agent is a lens, not an actor.

### 4.2 Group vs private is not a session distinction

Once the agent inherits the asker's permissions, "what can it see" is answered by RLS.
A group turn sees group content; a private turn sees that member's own thread plus
group plus wiki — because that is what the member can see.

The boundary is the person, not the channel. This collapses what looked like two
subsystems into one.

### 4.3 One agent turn at a time per room

Two simultaneous runs in one room means two agents that cannot see each other:
duplicated work, contradictory answers, and a race on the consent queue. It stops
reading as "one teammate."

Serialize with a Postgres advisory lock keyed on `team_id`. Private threads still run
in parallel — they share no surface.

**The queue already exists.** Messages are persisted *before* the turn runs
(`server/app.py:176`), so "what arrived while I was thinking" is just the room log
after the run's start time. No queue table needed.

### 4.4 Mid-turn injection is deferred

Claude Code folds messages that arrive mid-turn into the running turn at the tool
boundary (`query.ts:1568`). ADK supports the equivalent via `before_model_callback`,
which receives a mutable `llm_request` before each model call — so it is buildable.

But a merged turn has two drivers, and under §4.1 ("the agent acts as whoever is
driving it") there is no coherent answer to whose permissions apply, or whose consent
inbox a resulting proposal lands in. Revisit with an explicit rule.

Serialization (§4.3) is the first half of this anyway — same lock, same source.

### 4.5 Private memory is a scope, not a subsystem

Do not build a parallel per-member memory store. Add a `scope` column to
`memory_pages`: team / personal / restricted. Same pages, same versioning, same
citations, same one-tap revert.

Confirmed independently by two external architectures (§6).

---

## 5. Missing tools

The tool layer is a bigger gap than the loop. The current set is five
(`agent/tools.py`): `team_get_state`, `memory_read_page`, `team_propose_task`,
`team_propose_group_message`, `member_send_nudge`.

| Tool | Why | Claude Code analogue |
|---|---|---|
| `messages_search` | **The agent cannot read chat at all.** It answers about a room it cannot see | Grep |
| `document_read` | Documents feed the wiki but the agent cannot open one | Read |
| `task_get` / `task_propose_update` | It can only *create* tasks — never amend, reassign, or close | Edit |
| `member_activity` | "Who is stuck" has no signal source | — |
| `now()` | No clock; every deadline judgment is a guess | — |
| `memory_search` | Needed once the wiki outgrows an always-in-prompt index (~30 pages) | Glob + Grep |
| `propose_batch` | Multi-step work produces five separate consent cards today | — |
| subagent / delegate | "Sweep the team" grows context linearly with member count | Task tool |

Consent executors are a Python allowlist (`shared/consent.py:135`) containing only
`task_create` and `post_group_message`. Any new gated tool needs an executor **and** a
tier floor in `_TOOL_TIER_FLOORS`.

---

## 6. Memory architecture assessment

### 6.0 What Comrade has

Two-stage compiler (`pipeline/compiler.py`):

```
extract (source only, no wiki context)  →  consolidate (whole wiki in context)  →  apply (pure DB writes)
```

Stage 1 sees no existing wiki, so recall does not degrade as memory grows. Stage 2 has
four verbs — `add` / `revise` / `invalidate` / `noop` — plus page routing. Stage 3 is
deterministic and unit-tested, running in the caller's transaction.

Storage is bi-temporal: `memory_entries` is stable identity, `memory_versions` is
history (`is_active`, `valid_from`, `valid_until`). Invalidation writes a tombstone
version that is never active, so history shows why a fact died.

Two intake paths share one compiler: documents, and group chat (`pipeline/chat.py`,
debounced at ≥5 messages with a `chat_through` watermark).

**Genuine strengths**, several of which exceed what the external systems describe:

| Strength | Detail |
|---|---|
| Citations enforced at DB level | Every version cites source + verbatim excerpt; a trigger enforces that the citation's source belongs to the same team (`20260719090000_production_hardening.sql:42`) |
| Sole writer by grant, not policy | Only `comrade_pipeline` may write `memory_*`. The agent is read-only |
| Real revert | `memory_reverts` is member-insertable — one-tap undo |
| Injection defense | Spotlight/datamarking (`SPACE_MARK`) before every LLM call; chat is treated as untrusted input |
| Private never compiled | Group + human + undeleted only, enforced in the fetch SQL (`chat.py:31`) |
| Ambient capture | The worker sweeps; nobody has to ask |

### 6.1 vs PromptQL

Source: a July 2026 crawl of promptql.io. **Marketing claims, unverified.** The
principles are useful; the capability claims are not evidence.

| Dimension | Comrade | PromptQL (claimed) |
|---|---|---|
| Model | Wiki pages of cited facts | Same — "Wikipedia-style" |
| Versioning | Bi-temporal, tombstones | Revision history |
| Citations | **DB-trigger enforced** | Claimed |
| Revert | One tap, member-insertable | Communal correction |
| Write gate | Auto-write, then diff card | **Propose → human clicks "Add to wiki"** |
| Context kinds | Facts only | **Skills + knowledge + semantic layer** |
| Scopes | None | **Customer / internal / personal / confidential** |
| Page links | None | Interconnected pages |
| Conflict capture | None | Ambiguity + conflicts explicit |
| Rot detection | None | Re-pulls stale sources, proposes a fix |
| Bootstrap | Starts empty | Seeds from connectors |
| Injection defense | **Explicit spotlighting** | Not mentioned |
| Writer isolation | **Separate DB role** | Not mentioned |

**The load-bearing idea** is their user-identity essay, not their memory model:

> "If you can't see it, the agent can't see it for you."

They name agent identity — a bot with its own fixed permission set — as the wrong turn.
That is exactly what `comrade_agent` is today, and it is why §2.1 exists. This drove
decision §4.1.

Comrade's write-then-undo model (vs their propose-then-accept) is a *deliberate*
choice, not an oversight: it matches the T2 consent tier (act + visible card + one-tap
revert). Worth keeping, worth knowing it is a weaker trust story.

### 6.2 vs TencentDB-Agent-Memory

Source: repo README and GitHub metadata, fetched 2026-08-12. Not a source read.

TypeScript, Node ≥22.16. **Created April 2026** — four months old, 551 open issues,
~20k stars. License field resolves to `NOASSERTION` (the README claims MIT). Storage is
**SQLite + sqlite-vec** by default, with a Tencent vector DB alternative. Ships as an
OpenClaw plugin, a Hermes gateway adapter, or a Node library.

Architecture:
- Four layers: **L0 conversation → L1 atom → L2 scenario → L3 persona**
- Four asset types: Chat Memory, **Skills**, Wiki, CodeGraph
- Retrieval: BM25 + vector + RRF, capped by item count, character budget, and timeout
- Access: Private / Team / Restricted (ACL)

**Not adoptable as a dependency:**

| Blocker | Why |
|---|---|
| **SQLite, not Postgres** | 🔴 Decisive. Comrade's entire security model is DB-enforced RLS. Memory in a separate SQLite store sits outside that boundary; tenancy would have to be reimplemented in a second system |
| TypeScript | The backend is Python — would need a sidecar process |
| `NOASSERTION` license | Legal ambiguity for a product |
| Four months old, 551 open issues | Star count is not maturity |
| CodeGraph-centric | Built for coding agents. Comrade is not one |

**Worth stealing (design only):**

1. **The L3 persona layer.** Mapping Comrade onto their layers:

   | Layer | Comrade |
   |---|---|
   | L0 conversation | ✅ `messages` |
   | L1 atom | ✅ `memory_versions` (cited facts) |
   | L2 scenario | ✅ `memory_pages` (topic grouping) |
   | **L3 persona** | ❌ **nothing** |

   L3 is a long-term profile per person or team. This is the clearest framing yet of
   what "private memory" should be, and it also feeds the `member_activity` gap:
   **private memory = the L3 layer scoped to one member**, not a separate wiki.

2. **Skills as a first-class asset.** Now confirmed by *two* independent sources
   (PromptQL's "skills + knowledge + semantic layer"; this project's Chat/Skill/Wiki/
   CodeGraph). Comrade stores facts only — no procedural memory, no "how we do X".
   This promotes a `skill` page kind from optional to warranted.

3. **Retrieval budget caps** — item count, character budget, timeout. Comrade sends the
   whole wiki into every consolidation, unbounded. This is the concrete mechanism for
   the memory-cost ceiling flagged in the 2026-07-21 checkpoint.

4. **Hybrid retrieval (BM25 + vector + RRF)** — for when the wiki outgrows
   whole-wiki-in-context. **Postgres does all of this natively:** `tsvector` +
   `ts_rank_cd` for lexical, `pgvector` for semantic, and RRF is one CTE of
   `row_number()`. Zero new dependencies, and it runs inside RLS and inside the
   existing transaction.

**Skip:** CodeGraph, the SQLite backend, the plugin packaging, the library itself.

**Verdict: steal the model, not the code.** Comrade's storage layer is already stronger
than either external system for its own threat model. What it lacks is *layering* and
*retrieval discipline* — both buildable in Postgres.

### 6.3 Memory gaps, consolidated

1. 🔴 Page descriptions never written (§2.3) — cheapest high-value fix in the system
2. No L3 / persona layer → no per-member profile
3. Facts only — no skills, no procedural memory
4. No scopes — team-wide or nothing
5. No cross-page links
6. No conflict state — disagreement collapses to `revise`, the losing side vanishes
7. No staleness detection
8. Unbounded consolidation cost — whole wiki into every call
9. No bootstrap — the wiki starts empty
10. `page_index()` is dead code (§2.4)

---

## 7. Open questions

- **What does a queued member see?** When Sam invokes Comrade while Maya's turn is
  running: an honest "Comrade is on Maya's question — yours is next," or a plain
  thinking indicator?
- **Whose consent inbox** receives a proposal from a turn with multiple contributors?
  (Blocks §4.4; the `consent_queue.agent_run_id` column is a partial answer.)
- **Session granularity for private threads** — one persistent ADK session per
  (team, member), or per conversation?

---

## 8. Suggested order

Ordered by what unblocks what, not by size.

1. **§2.1 — reads run as the requester.** Do this *before* writing any message-reading
   tool. Deletes code.
2. **§2.2 — unique constraint on `action_hash`.** One line.
3. **§2.3 — write page descriptions.** Unblocks the recall design already built.
4. **Connection pool** (`psycopg_pool.ConnectionPool`, one per role, module-level;
   `team_session` takes from the pool). Biggest single win — fixes the connection
   problem everywhere at once.
5. **`agent_steps` table.** Kills the jsonb rewrite, the per-step connection, and the
   `agent_runs` bloat; unblocks `parent_run_id` and queryable steps.
6. **Session storage + `RunConfig` + a permission plugin.** Turns on conversation
   memory and turn caps. Needs the dedicated schema for ADK's tables.
7. **Cheap columns migration** — tokens/cost on `agent_runs`; `batch_id` and
   `agent_run_id` on `consent_queue`; `cancelled` status; `agent` trigger type.
8. **Fix the chat sweep query** — partial index, or a per-team watermark row instead of
   recomputing by scan.
9. **`messages_search` + `document_read`** — the two tools that most change what the
   agent can do. Requires `documents.parsed_text` and a `tsvector` index.
10. **Room advisory lock** (§4.3).
11. **`scope` column on `memory_pages`** (§4.5) + `skill` page kind (§6.2).
12. **Consolidation context cap** (§6.2 item 3).

Deferred: `idx_messages_private_owner` sort column, autovacuum tuning on `jobs`,
uuidv7 for new tables, hybrid retrieval, cross-page links, conflict state, rot
detection, partitioning.

**Migration policy from now on:** `CREATE INDEX CONCURRENTLY` on existing tables;
check constraints as `NOT VALID` then `VALIDATE`.

> §9–§11 were appended later the same day (session 2). §11 supersedes the order above.

---

## 9. Claude Code permission model — comparison

> Source: a local copy of the leaked Claude Code CLI source. Read for
> architecture only; nothing was copied. Its behaviour is what the code says —
> not verified by running it. Files read: `src/utils/permissions/permissions.ts`,
> `Tool.ts`, `src/services/tools/{toolExecution,toolHooks}.ts`,
> `src/hooks/toolPermission/{PermissionContext,handlers/interactiveHandler}.ts`,
> `src/utils/permissions/{denialTracking,dangerousPatterns,pathValidation,PermissionUpdateSchema}.ts`,
> `src/components/permissions/*`.

### 9.1 Their model: one mandatory chokepoint

`canUseTool` sits between "model emitted a `tool_use` block" and "tool runs". The
model never dispatches a tool itself, so the gate cannot be skipped by anything
the model emits.

Inside it, one ordered, deny-biased function (`hasPermissionsToUseToolInner`):

| Step | Check | Result |
|---|---|---|
| 1a | tool-level deny rule | deny |
| 1b | tool-level ask rule | ask |
| 1c | `tool.checkPermissions(input, ctx)` | tool-specific |
| 1d | tool returned deny | deny |
| 1e | `requiresUserInteraction()` | ask |
| 1f | content-specific ask rule | ask |
| 1g | safety check (`.git/`, `.claude/`, shell configs) | ask |
| 2a | `bypassPermissions` mode | **allow** |
| 2b | always-allow rule | allow |
| 3 | nothing matched (`passthrough`) | **ask** |

Four properties worth naming:

1. **Ordering is the enforcement.** Full-bypass mode sits at 2a, *after* every
   deny and safety check, so it is structurally incapable of overriding them. No
   flag or exception clause — just statement order in one function.
2. **Unknown means ask** (step 3). Default-deny by construction.
3. **Conservative type defaults.** `Tool.ts` defaults are `isConcurrencySafe →
   false`, `isReadOnly → false` ("assume writes"). Forgetting to declare gets the
   dangerous assumption.
4. **Escape hatches are themselves gated.** A PreToolUse hook may approve a tool,
   but hook-allow still runs `checkRuleBasedPermissions`, so deny/ask rules
   survive it (`toolHooks.ts:373`).

### 9.2 Their approval flow vs ours

The structural difference everything else follows from:

- **Claude Code — blocking prompt.** The turn *pauses*. `handleInteractivePermission`
  returns nothing; it installs callbacks and the tool call sits frozen awaiting a
  promise. The click resolves it and the same call stack continues into the tool.
- **Comrade — durable work item.** The turn *ends*, a row is written, approval
  arrives later in a separate request, and a different principal executes it.

Neither is wrong. Ours has to be a queue: approval may come hours later, from a
different person, on a different device.

| Comrade step | Claude Code equivalent |
|---|---|
| Model calls a gated tool | Same — but *the gate* decides gatedness, not the tool's own body |
| Tier floor applied | No tiers. One three-value answer: `allow` / `deny` / `ask` |
| `action_hash` stamped | Nothing — zero time passes, nothing can drift |
| Pending row in Postgres | In-memory object on `toolUseConfirmQueue`. Dies with the process |
| Card shows literal args | Same rule; per-tool renderers (Bash shows the command, FileEdit a diff) |
| Approval runs as the human (RLS authorises) | No authorisation step — there is only one human |
| T3 second key | Nothing. Single-player |
| CAS claim (exactly-once) | `createResolveOnce.claim()` — same idea, in a closure |
| Four gates after the claim | Nothing to re-gate |
| Executor writes as a different principal | Same process, same permissions |

**Confirmed strengths (keep):** our hash / expiry / precheck machinery and the
executor role exist *because* approval is deferred and multi-party. They have no
analog there because their gap is milliseconds. The gate living in a `GRANT`
rather than a function is something Claude Code structurally cannot do — it edits
files as the user, with no lower authority to appeal to.

**Third independent confirmation of §4.1:** Claude Code has no "agent identity
with its own permission set" — every decision is made against *the user's* rules
and *the user's* working directory. After PromptQL (§6.1), that is two external
systems landing on the same answer.

### 9.3 Gaps found

| # | Gap | Severity |
|---|---|---|
| G1 | **No mandatory chokepoint.** Gating is per-tool convention: `team_propose_*` call `propose_action`; `member_send_nudge` acts directly (`agent/tools.py:200`). A sixth tool that forgets to route through `propose_action` is ungated, and nothing catches it. DB grants are the backstop but are coarse — they say "cannot write tasks", not "this tool needs consent". Anything the agent role *is* granted (e.g. private-message insert) is ungated by default | 🔴 |
| G2 | **Unknown defaults to "whatever the body does"**, not to "ask". No `passthrough → ask` equivalent | 🔴 |
| G3 | **Rejection never reaches the model.** `reject_consent` sets status and returns; the turn ended long ago. The agent never learns it was rejected, or why, and will re-propose. Claude Code returns the rejection *with the user's typed reason* into the tool result | 🔴 |
| G4 | **Approval does not accumulate.** One approval = one instance, forever. Claude Code's dialog returns `PermissionUpdate[]` (`addRules` / `setMode` / `addDirectories`) with a destination (`session` \| `userSettings` \| `projectSettings` \| `localSettings`) — that *is* the "don't ask again" system, and it is what the earned-trust ratchet in `comrade-platform-findings.md` §Part 5 describes | 🟡 |
| G5 | **No circuit breaker.** Their `DENIAL_LIMITS = {maxConsecutive: 3, maxTotal: 20}` (`denialTracking.ts`) is numerically identical to the ratchet our own governance ruling calls for. Unbuilt. The hourly 429 is a *cost* ceiling, not a *trust* ceiling | 🟡 |
| G6 | **No re-check when policy changes.** Their `recheckPermission()` re-runs the whole pipeline while a prompt is open, so a pending item auto-resolves if the rules moved in its favour. Ours re-verifies the mirror case (the *action* changed, via `action_hash`) but never the *rules* changing | 🟡 |
| G7 | **Tools do not self-declare.** No `isReadOnly` / `isDestructive` / `requiresUserInteraction` on Comrade tools — there is no field to be conservative *about*. `_TOOL_TIER_FLOORS` has two entries and is read only from inside `propose_action` | 🟡 |
| G8 | **Single-channel approval.** Their claim races four responders (terminal, hook, classifier, remote/phone); first to `claim()` wins, rest no-op. Our CAS guards double-*execution* only. Needed before an Agent Inbox or mobile approval | 🟢 |
| G9 | **No sanitisation of over-broad policy.** `dangerousPatterns.ts` strips user-authored allow-rules that would hand over an interpreter (`Bash(python:*)` = arbitrary code). Not applicable yet — we have no user-authored rules — but becomes live the moment per-team autonomy policy ships | 🟢 |

### 9.4 Over-complications

| # | Finding |
|---|---|
| O1 | **Tier semantics live in four layers** — `_TOOL_TIER_FLOORS` (Python), a check constraint, the second-key trigger, and `consentPhase()` (TypeScript, **eight** display phases). Their whole vocabulary is three behaviours plus composable rules. The 8-phase state machine is the symptom: the UI must re-derive tier meaning from raw columns because it is not expressed in one place. Largely resolved by §10 |
| O2 | **Authorisation re-derived per statement.** One `/agent/turn` opens three connections before Gemini is called (`require_membership`, `_check_turn_budget`, `_persist_user_message`). Claude Code resolves its permission context once and passes it down. Same root cause as the connection-pool item in §3.2 — the *architecture* (RLS as authorisation) is right; the *implementation* re-establishes it per statement |
| O3 | **`action_hash`'s docstring argues against itself.** It states it is not anti-tamper (true — the role split is). Its actual value is catching a code path that mutates `tool_args` without re-stamping, and there are now two such paths. Keep the mechanism; fix the rationale |

---

## 10. Decision: T3 and the two-key system are removed

**Owner decision, 2026-08-12.** The T3 tier and the entire countersign mechanism
come out. This reverses the provisional governance ruling logged 2026-07-19
(`20260719130000_consent_tiers.sql`).

**Effect.** The consent queue then holds exactly one shape — "needs the
requester's key" — which collapses O1: `consentPhase()` loses four of its eight
phases, `consent_queue` loses two columns, one constraint, two policies and one
trigger, and the flow in `shared/consent.py` becomes propose → approve →
execute with no waiting state.

**Open question to settle before executing:** does the `tier` column survive?

- **Recommended:** keep `tier` as an informational label with the constraint
  narrowed to `('T0','T1','T2')`. It is one cheap column, it keeps `_TOOL_TIER_FLOORS`
  meaningful for the autonomy classifier in §9.3 G4/G5, and dropping a column is
  a harder migration to reverse than narrowing a check.
- **Alternative:** drop `tier` entirely and let the tool registry (§11.1) carry
  risk. Cleaner, but discards the seed for the earned-trust ratchet.

### 10.1 Removal manifest

Every touchpoint, verified by grep 2026-08-12. Nothing here is done yet.

| Layer | File | What goes |
|---|---|---|
| Migration (new) | `supabase/migrations/2026________.sql` | drop policies `au_consent_queue_select_t3`, `au_consent_queue_second_key`; drop trigger `trg_consent_second_key`; drop function `trg_consent_second_key_guard`; drop constraint `consent_second_key_distinct`; drop columns `second_approver_id`, `second_approved_at`; narrow the `tier` check |
| Python | `shared/consent.py` | `add_second_key()` (whole function); the T3 branch in `execute_consent` (:119-126); the T3 branch in `approve_consent` (:164-165); `"T3"` from `_TIER_ORDER`; docstring at :150-152 |
| API | `server/app.py` | `POST /consent/{id}/second_key` (:252-265); the `add_second_key` import (:29) |
| Frontend | `src/lib/consentModel.ts` | the whole `item.tier === 'T3'` block (:27-37); phases `awaiting_second_key`, `countersigned_pending`, `can_countersign` |
| Frontend | `src/lib/agentApi.ts` | `secondKeyConsent()` (:92-95) |
| Frontend | `src/components/ConsentCard.tsx` | the three T3 badges (:41-46); the countersign button block (:323-345) |
| Frontend | `src/screens/ConsentInbox.tsx` | the T3 comment + "still live" clause (:17-18, :33-38); the T3 legend row (:75) |
| Frontend | `src/lib/types.ts` | the tier comment (:174) and `second_approver_id` on `ConsentItem` |
| Tests | `tests/test_consent_tiers.py` | ~10 of 18 tests (the T3 gate + countersign-integrity blocks, :42-153). Keep the floor tests |
| Tests | `tests/test_server.py` | `test_second_key_passes_the_caller`, `test_second_key_not_countersignable_is_404` (:165-183) |
| E2E | `frontend/tests/e2e/global-setup.ts` | the seeded T3 item (:80) and the two-context countersign journey |
| Docs | `HANDOFF.md` | §7 governance rulings (:156-159), gap 0b (:124) |

*(`tests/rls_isolation_test.sql:106` matches "T3" but is an unrelated test label — leave it.)*

---

## 11. Revised order

> ⚠️ **Superseded by §19** (Addendum 2026-08-18). Kept for the reasoning; use §19
> for sequencing.

Supersedes §8. Ordered by what unblocks what.

1. **§2.1 — reads run as the requester.** Before any message-reading tool. Deletes code.
2. **§10 — remove T3 + the two-key system.** Deletes code across four layers and
   collapses O1. Do it before the chokepoint work so the gate is written against
   the final consent shape, not the old one.
3. **§9.3 G1/G2 — the tool chokepoint.** ADK's `BasePlugin.before_tool_callback`
   is the installed, unwired equivalent of `canUseTool` (§1). A tool registry
   declaring each tool `read` / `act` / `propose`, with **unknown defaulting to
   propose**, plus the plugin that enforces it. Small — the value is that it
   fails closed for tools nobody classified.
4. **§2.2 — unique constraint on `action_hash`.** One line.
5. **§2.3 — write page descriptions.** Unblocks the recall design already built.
6. **Connection pool** (§3.2, and O2). Biggest single win; fixes the connection
   problem everywhere at once.
7. **§9.3 G3 — rejections reach the model.** Carry rejected/expired proposals and
   their reason into the next turn's context. Requires a reason field on
   `consent_queue` and a rejection note in the turn prompt. Highest
   product value per line in this list — today the agent cannot learn inside a session.
8. **`agent_steps` table.** Kills the jsonb rewrite, the per-step connection, the
   `agent_runs` bloat; unblocks `parent_run_id`.
9. **Session storage + `RunConfig` + the permission plugin proper.** Conversation
   memory and turn caps. Needs the dedicated schema for ADK's tables.
10. **Cheap columns migration** — tokens/cost on `agent_runs`; `batch_id`,
    `agent_run_id` on `consent_queue`; `cancelled` status; `agent` trigger type.
11. **Fix the chat sweep query.**
12. **`messages_search` + `document_read`.** Requires `documents.parsed_text` and
    a `tsvector` index.
13. **Room advisory lock** (§4.3).
14. **§9.3 G4/G5 — standing approvals + circuit breaker.** Together they are the
    earned-trust ratchet. Build when proactive turns land — a reactive agent that
    only speaks when spoken to cannot nag, so the brake is not yet load-bearing.
15. **`scope` column on `memory_pages`** (§4.5) + `skill` page kind (§6.2).
16. **Consolidation context cap** (§6.2 item 3).

Deferred, unchanged from §8: `idx_messages_private_owner` sort column, autovacuum
tuning, uuidv7 for new tables, hybrid retrieval, cross-page links, conflict state,
rot detection, partitioning. Newly deferred: G6 (policy re-check), G8 (racing
approvers — revisit with the Agent Inbox), G9 (allow-rule sanitisation — revisit
with per-team autonomy policy).

---
---

# Addendum — 2026-08-17

> Session output: a full call-graph trace of "when does the agent decide to post",
> the owner decision that followed, and a removal manifest. Complements §10 (T3
> removal) — both are logged-not-executed, and they overlap in three test files.
>
> Verification method stated per finding. Everything in §13.2 was grep-verified
> on 2026-08-17 against the working tree at `feat/frontend`.

---

## 12. Finding: the agent has no autonomous trigger at all

**Verified by exhaustive call-graph trace, 2026-08-17.** §11 item 14 already
noted in passing that "a reactive agent that only speaks when spoken to cannot
nag." This section establishes it as a hard property of the current code, not an
observation, because it changes what the consent gate is actually protecting.

### 12.1 Three invocation paths, all human-initiated

| Call site | Trigger | Identity source |
|---|---|---|
| `server/app.py:145` `POST /agent/turn` | human request | JWT `sub` |
| `server/app.py:183` `POST /agent/turn/stream` | human request | JWT `sub` |
| `scripts/smoke_runtime.py:31` | dev script | hardcoded |

There is no fourth. `pipeline/worker.py` registers exactly two handlers —
`parse_document` (`compiler.py:442`) and `compile_memory` (`chat.py:193`) — and
neither invokes the agent. No scheduler, no cron, no APScheduler/Celery anywhere
in the tree.

**Consequence:** `team_propose_group_message` is reachable *only* mid-turn, inside
a turn a human started. The agent cannot volunteer a group post because it is
never running when nobody asked.

### 12.2 `trigger_type` has two dead enum values

`agent_runs.trigger_type` is constrained to `('user','document','scheduled')`
(`20260612094142_init.sql:272`). `trigger_type` defaults to `"user"` at
`agent/runtime.py:58` and **nothing in the tree ever passes anything else** —
`'document'` and `'scheduled'` are provisioned but never written.

⚠️ **Easy misread:** `pipeline/chat.py:180` passes `trigger="scheduled"`, but that
writes `memory_compilations.trigger`, *not* `agent_runs.trigger_type`. Ambient
*compilation* is genuinely scheduled; ambient *agent behaviour* is not. Different
table, different column, same word.

### 12.3 The proactive-observation machinery has no producer

A complete suppression system exists for a message category nothing emits:

| Piece | State |
|---|---|
| `observation_suppressions` table + `kind` column (`20260719130000:99`) | built |
| `POST /observations/{id}/suppress` (`server/app.py:294`) | built |
| `tombstone_ai_message()` definer function (`20260719160000:27`) | built |
| Frontend button, `kind` hardcoded `'proactive_observation'` (`GroupRoom.tsx:119`) | built |
| **Anything that generates a proactive observation** | **does not exist** |

Second-order gap: `grant select on public.observation_suppressions to
comrade_agent` exists (`20260719130000:122`) but **no tool queries the table**.
Even once a producer lands, the "don't do this again" signal is not wired into
the agent's context — the suppression is recorded and then ignored.

### 12.4 What the consent gate was therefore protecting

With no autonomous trigger and no when-condition in the instruction (see §13.1),
the only realistic path that fires `propose_group_message` is a member saying
*"tell everyone the deadline moved."* The gate then reduces to:

> member asks the AI to broadcast → AI proposes the broadcast → card asks the
> member to approve the thing they just asked for

This is the consent-fatigue risk from `comrade-platform-findings.md:241` in its
most degenerate form: the gate guards a door only the asker can open, so its cost
is live today while its benefit arrives only with proactive turns. It is the
direct justification for §13.

---

## 13. Decision: the agent never initiates a group post

**Owner decision, 2026-08-17.** The agent's ability to propose a group-room post
is removed outright. Group content originating from AI work enters the room only
when a human publishes it, under their own name.

### 13.1 What the rule is, and what it is not

The rule is about **initiation**, not about AI text in shared space. Owner's
wording: *"other than the time when the user is interacting with comrade, the
agent should not post anything by itself."*

| Behaviour | Verdict | Why |
|---|---|---|
| `@comrade` in the group room → reply in the room (`_persist_ai_reply`, `app.py:93`) | **keep** | Human pulled the AI in; the answer reaches exactly the people who saw the question |
| Memory diff card posted by the compiler (`apply_compilation`, `compiler.py:324-329`) | **keep** | Treated as a system notice, not the AI talking; it is also the transparency that substitutes for a memory approval gate (`app.py:314`) |
| `team_propose_group_message` — AI decides a post is warranted | **remove** | The only path where the agent originates group content |
| Repo events, tool status, scheduled summaries → group room | **never build** | Explicitly excluded by the decision, including future connectors |

So the two kept cases are *not* exceptions to the rule — neither is the agent
acting by itself. `propose_group_message` was the only one that was.

**Root cause worth recording:** the tool had no trigger condition to remove.
`agent/agent.py:49` says only *"To post to the group room, use
team_propose_group_message"* — no when, no scenario. Contrast `member_send_nudge`
three lines below, which says *"keep it to the situations the nudge types
describe"* and is backed by four enumerated types. The group-post tool was an
ungoverned capability; the nudge tool is a scoped one. Removing the ungoverned
one is cheaper than inventing the governance it never had.

### 13.2 Removal manifest

Grep-verified 2026-08-17. Nothing here is done yet.

| Layer | File | What goes |
|---|---|---|
| Python | `agent/tools.py` | `team_propose_group_message()` (:177-197) |
| Python | `agent/agent.py` | import (:14); tools-list entry (:101); rewrite instruction (:49-51) and the "You never post to the group" clause (:54-55) — after removal the agent has no group-post tool to disclaim |
| Python | `shared/consent.py` | `"post_group_message": "T2"` floor (:35); `_precheck_post_group_message` (:248-256); `_exec_post_group_message` (:258-265); both dict entries (:270, :274) |
| Migration (new) | `supabase/migrations/2026________.sql` | `revoke insert on public.messages from comrade_executor` — see §13.3 |
| Tests | `tests/test_agent.py` | `"team_propose_group_message"` from the expected-tools set (:17) |
| Tests | `tests/test_consent_loop.py` | `test_propose_group_message_approve_posts` (:72-98), whole test |
| Tests | `tests/test_consent_tiers.py` | `_propose_t3` vehicle (:21) and the floor assertion (:29) must switch tools — see §13.5 |
| Frontend fixtures | `ConsentCard.test.tsx` (:21, :47), `e2e/global-setup.ts` (:73-82), `e2e/journeys.spec.ts` (:91), `integration/rls-consent.test.ts` (:23, :30, :83) | stale tool-name literals — see §13.5 |
| Docs | `README.md` | see §13.6 |
| Docs | `docs/architecture.md` | §5 consent section names `post_group_message` as one of two executors |
| Docs | `HANDOFF.md` | §3 API surface and §7 governance rulings both cite the group-post proposal |

### 13.3 Two consequences that are not obvious from the diff

**1. `comrade_executor` loses its last use for `messages`.** `Role.EXECUTOR` is
opened at exactly one place (`shared/consent.py:101`). After removal
`_EXECUTORS` holds only `task_create`, whose executor writes `tasks` and nothing
else. So `grant insert on public.messages to comrade_executor`
(`20260612120000:45`) becomes dead privilege and should be revoked in the same
migration. Least privilege is the whole point of the role split; leaving a
granted-but-unused write is the kind of thing that silently becomes reachable
again later.

**2. `reversible` degenerates to always-true.** It is currently set `True` by
`team_propose_task` and `False` only by `team_propose_group_message`
(`tools.py:173, :196`). After removal every proposal is reversible, so
`ConsentCard.tsx:128-133` will render the `REVERSIBLE` badge unconditionally and
the `IRREVERSIBLE` branch becomes unreachable. **Do not drop the column** — it is
read by the frontend and by the audit trigger (`20260719130000:79, :83`), and
§10's reasoning applies (narrowing beats dropping). Note the badge as cosmetically
dead until a genuinely irreversible tool is registered.

### 13.4 New capability: publish an artifact from the private thread

**Owner decision, 2026-08-17.** A member working with Comrade in their private
thread gets the option to publish that work into a team they belong to.

Chosen shape: **the member's own message, carrying an AI-derived marker.**

| Aspect | Decision |
|---|---|
| Attribution | `sender_kind='user'`, `sender_id = auth.uid()` — the member owns it and stands behind it |
| Provenance | one boolean column, `messages.ai_assisted`, rendered as a small "drafted with Comrade" marker |
| Transport | **plain `supabase.from('messages').insert()` from the browser** — no API endpoint, no consent path |
| Consent | none required; a human composing and sending their own message is not a gated action |

**Why no backend is needed** — `au_messages_insert`
(`20260612095500_rls.sql:128-131`) already requires exactly
`sender_kind='user' and sender_id = (select auth.uid())`. RLS policies gate rows,
not columns, so a member may set `ai_assisted` on their own insert with no policy
change. Verified against the policy text 2026-08-17.

Build list:

| Layer | File | What |
|---|---|---|
| Migration (new) | `supabase/migrations/2026________.sql` | `alter table public.messages add column ai_assisted boolean not null default false`; consider `check (not ai_assisted or sender_kind = 'user')` — an AI message claiming to be AI-assisted is nonsense |
| Frontend | `src/lib/types.ts` | `ai_assisted: boolean` on `Message` (:36-48) |
| Frontend | `src/screens/PrivateThread.tsx` | per-AI-message "publish to team" affordance; prefill an **editable** body so the member can trim before it carries their name |
| Frontend | `src/screens/GroupRoom.tsx` | marker in `MessageRow`'s badge row (:363-378), alongside the existing `AI · SEEN BY ALL` badge |

Deliberately **not** decided yet: whether the target team can differ from the
current room (owner said "his chosen associated team", which implies a picker
across the member's memberships, not just the active team). Flagged rather than
assumed — a cross-team publisher needs its own RLS thinking, since
`is_team_member` is checked against the *target* `team_id`.

No text-composition helper is needed: the body is the AI text verbatim (or the
member's edit of it), and provenance is carried by the column rather than by
prefixing quoted text. Nothing to unit-test beyond the insert itself, which the
existing `rls-messages.test.ts` pattern covers.

### 13.5 The test suite barely notices this removal

**This is the main risk in executing §13.2.** Of the ten test references to
`post_group_message`, only **two** go red:

| Test | Outcome after removal | Why |
|---|---|---|
| `test_agent.py::test_tools_registered` | 🔴 red | asserts the tool name is in `root_agent.tools` |
| `test_consent_loop.py::test_propose_group_message_approve_posts` | 🔴 red | approves, then expects `"executed"`; `execute_consent` will raise `ConsentError("no executor registered for post_group_message")` |
| `test_consent_tiers.py::test_tier_floors_cannot_be_lowered` | 🟢 **green for the wrong reason** | asserts `resolve_tier("post_group_message","T0") == "T2"`; with the floor deleted the tool is unknown, and `DEFAULT_TIER` is also `"T2"` |
| `test_consent_tiers.py` T3 helper + visibility test | 🟢 stale | `propose_action` does **not** validate `tool_name` against `_EXECUTORS`; it only inserts. Proposals for nonexistent tools succeed |
| 4 frontend fixture files | 🟢 stale | DB/UI fixtures that never execute the tool |

The load-bearing fact is the middle two rows: **`propose_action` performs no
tool-name validation**, so eight of ten references keep passing while referring to
a capability that no longer exists. Anyone reading a green suite after this change
would reasonably conclude the tool is still supported.

Two follow-ups, both cheap:

1. Switch the surviving fixtures to `task_create` (or a deliberate
   `"fixture_tool"` sentinel) so no test names a removed capability.
2. Consider validating `tool_name` against `_EXECUTORS` **at propose time**, not
   just execute time. Today a typo'd or removed tool produces a pending consent
   card that can never execute — it fails only when a human approves it, which is
   the worst moment to discover it. This is a natural companion to the §11 item 3
   tool registry (unknown → propose), and would have turned this whole removal
   into a red suite.

### 13.6 Doc bug introduced 2026-08-17 (mine, not pre-existing)

`README.md` was updated earlier the same day and states:

> **The agent never performs a group-visible write.**

**False as written.** `_persist_ai_reply` (`server/app.py:93`) inserts into
`messages` with `thread_type='group'` under `Role.AGENT`, ungated. The base grant
`grant insert, update on public.messages ... to comrade_agent`
(`20260612095500_rls.sql:231-233`) is only narrowed by `revoke update on
public.messages from comrade_agent` (`20260612120000:27`) — **INSERT on
`messages` is retained by the agent role.**

The corresponding line in `docs/architecture.md` — "no `INSERT` on `tasks` and no
`UPDATE` on `messages`" — is precise and correct. Only the README sentence
overclaims. Correct wording after §13 lands: *the agent never performs a
group-visible action it chose itself.*

For the record, the agent role's complete remaining write surface:
`insert` on `messages`, `consent_queue`, `agent_runs`, `change_log`, `nudge_log`
(`20260612120000:88`), plus `update` on `agent_runs` / `change_log`. `tasks` and
`milestones` are fully revoked (`20260612120000:24-25`), and `consent_queue` is
insert-only (`:26`) — propose, never resolve.

### 13.7 Interaction with §10, and where this lands in §11

**Sequencing.** §10 (remove T3) and §13 overlap in `tests/test_consent_tiers.py`,
`e2e/global-setup.ts` and `e2e/journeys.spec.ts`, because `post_group_message` is
the *vehicle* those tests use to exercise T3. Whichever lands second finds fewer
references — but §10 deletes ~10 of the 18 tier tests outright, so **doing §10
first removes most of §13.2's test work for free.** Do not reverse the order.

**Order.** §13 slots in immediately after §10 in the §11 list, before the tool
chokepoint (item 3): the registry should be written against the final tool set,
not one that still contains a capability being removed. It deletes code in three
Python files and revokes one grant.

**§11 item 14 is now firmer, not just deferred.** Standing approvals and the
circuit breaker were deferred on the grounds that a reactive agent cannot nag.
§12 proves that is structural, and §13 removes the one tool that could have
produced unsolicited group output. The earned-trust ratchet is therefore blocked
on proactive turns existing at all — and per §13.1, any future proactive work must
route to the private thread or a non-chat surface, never to the group room.

**Unchanged by this decision:** `member_send_nudge` still sends immediately, still
targets another member's private thread, and is still the agent's one ungated
write. It is out of scope here because it is private, not group — but the
asymmetry noted in §9 stands, and arguably sharpens: after §13 the agent may not
put a word in the shared room on its own initiative, yet may still DM a teammate
who never asked. Worth revisiting when the nudge producer moves off the
user-invoked turn.

---

# Addendum — 2026-08-18

> Session output: a read of Claude Code's tool layer, a comparison against Delta
> (Zed) and Tines 3B, and the design decisions for GitHub integration and the
> execution sandbox that follow from a positioning correction.
>
> Verification status marked per finding. Third-party product behaviour is that
> product's own claim (docs, blog, or leaked source read for architecture only),
> not verified by running it.
>
> §19 supersedes §11.

---

## 14. Positioning correction — the premise for §16 and §17

**Owner statement, 2026-08-18.** Comrade's primary audience is **developer and
engineering teams**. Connecting a repository is a core feature, not an
integration. Comrade is a **team harness** — it should do what Claude Code does,
for a team, with no loss of agentic capability. Comrade's own agent stays
primary; hosting external harnesses is not the v1 story.

**This is a clarification, not a pivot — the schema already assumed it.**
`github_repos` and `github_activity` landed in migration one
(`20260612094142_init.sql:222-244`) with the comment *"AI queries this, not the
raw repo."* `memory_citations.source_kind` already accepts `'github'`.
`contribution_v` already counts `github_events`. The memory-ingestion findings
already ruled *"Code (repo) → agentic live-read, not pre-embed."* What is
missing is that **nothing writes those tables** — which is why the contribution
screen's GitHub half has no data and the `idle` nudge type has no signal source.

**Stale records to correct:** `README.md:3` still opens *"An AI companion for
student group projects."* Project memory still records the target as "any small
team, students = pilot beachhead." Both now understate the product.

---

## 15. The tool layer

### 15.1 What Claude Code ships

Read from a local copy of the leaked source, architecture only. ~40 tool
directories in `src/tools/`, plus feature-gated ones. Model-facing names:

| Group | Tools |
|---|---|
| Core coding loop | `Read` `Write` `Edit` `NotebookEdit` `Glob` `Grep` `Bash` `PowerShell` `REPL` `Agent` `Skill` `LSP` |
| Web | `WebFetch` `WebSearch` |
| Delegation / tasks / teams | `TaskCreate` `TaskGet` `TaskList` `TaskUpdate` `TaskStop` `TaskOutput` `TeamCreate` `TeamDelete` `SendMessage` |
| Modes / workspace | `EnterPlanMode` `ExitPlanMode` `EnterWorktree` `ExitWorktree` |
| User-facing | `AskUserQuestion` `SendUserMessage` (`BriefTool`) `SendUserFile` `PushNotification` |
| MCP / discovery | `ToolSearch` `MCPTool` `McpAuth` `ListMcpResourcesTool` `ReadMcpResourceTool` |
| Scheduling / triggers | `CronCreate` `CronDelete` `CronList` `RemoteTrigger` `Monitor` `Sleep` |
| Bookkeeping | `TodoWrite` `Config` `StructuredOutput` |

### 15.2 Three registry mechanisms worth copying

1. **Deferred loading.** Roughly two-thirds carry `shouldDefer: true` — the
   schema is absent from the prompt until `ToolSearch` fetches it. The split is
   deliberate: the **core loop** is always loaded; everything **peripheral**
   (tasks, teams, cron, MCP, modes) is deferred.
2. **Availability is set-based per agent kind** (`src/constants/tools.ts`):
   `ALL_AGENT_DISALLOWED_TOOLS`, `ASYNC_AGENT_ALLOWED_TOOLS` (an *allowlist* of
   ~15), `IN_PROCESS_TEAMMATE_ALLOWED_TOOLS`. Note the asymmetry — **the more
   autonomous the agent, the more it is governed by an allowlist rather than a
   denylist.**
3. **Tools self-declare safety properties, defaulting to the dangerous
   assumption.** `Tool.ts:758-763`: `isConcurrencySafe → false`,
   `isReadOnly → false` ("assume writes"), plus optional `isDestructive`,
   `requiresUserInteraction`, `interruptBehavior`. These feed the permission gate
   *and* the parallel executor — which is why "read-only tools run in parallel"
   needs no special-casing.

### 15.3 Comrade's gap

Five tools (`agent/tools.py`), **zero declarations**. There is no field for a
chokepoint to read. This is §9 G7 restated with the reference implementation
attached.

The minimum viable harness tool set is six, not forty:

`Read` · `Write` · `Edit` · `Grep` · `Glob` · `Bash`

Everything else — subagents, skills, deferred loading, web — is elective and
should stay unbuilt until a real need appears.

### 15.4 Registry design

A registry keyed by tool name carrying three columns:

| Column | Values | Purpose |
|---|---|---|
| `surface` | `sandbox` \| `db` \| `outbound` | which boundary the call crosses |
| `writes` | bool | conservative default `true` |
| `needs_human` | bool | conservative default for anything `outbound` |

**Unknown tool → `outbound`, `writes=true`, `needs_human=true`.** Fails closed
for tools nobody classified, which is the entire point.

### 15.5 Chokepoint scope — refined

§11 item 3 said the ADK `before_tool_callback` gate becomes mandatory once
`Bash` exists. That is too broad. The correct rule, given §17:

> **The chokepoint governs tools that leave the sandbox. It does not govern the
> shell inside it.**

If the sandbox holds no credentials and cannot reach anything unproxied, there is
nothing inside it to gate — per-command approval buys nothing and costs the
capability the owner explicitly refused to trade away. Gate `surface=outbound`
and `surface=db`; let `surface=sandbox` run free.

---

## 16. GitHub integration

### 16.1 Three patterns in the market

| Pattern | Who | Mechanism |
|---|---|---|
| **Isolated branch + draft PR** | Copilot coding agent, Devin, Codex cloud | Agent confined to a branch namespace (`copilot/*`), cannot touch protected branches; ephemeral runner; opens a **draft** PR and iterates; branch protection, required checks and CODEOWNERS still apply |
| **Coordination layer that links, not writes** | Linear | Magic words in commits/PR bodies create bidirectional links; automations move issue status from PR state; **delegates** coding to external agents rather than being one |
| **Identity plumbing** | GitHub Apps | **Installation token** (server-to-server) acts as the app itself, expires in 1h. **User-to-server token** acts as a person, and the audit log names *them* as actor |

### 16.2 The load-bearing finding: the PR is already a consent gate

In Pattern A the agent writes freely into a namespace nobody has merged, and the
**merge is the human decision**. No approval dialog exists because the blast
radius of an unmerged branch is zero.

That is Comrade's own T2 definition — *shared but reversible → act, show a
visible card, offer one-tap revert*. So **repo writes need no consent-queue
round-trip**, provided they only land where nothing is merged.

**This closes the open question left by §10.** Merge, protected-branch write,
force-push, secrets and Actions config are the irreversible class. They need no
replacement for the removed two-key mechanism, because GitHub's branch protection
is a *stronger* second key than the countersign trigger being deleted — enforced
by the system that owns the resource.

Design rule: **don't ask, don't hold.** The App's permission set excludes
protected-branch writes entirely.

### 16.3 Attribution: the invariant is GitHub's auth model

*"The AI acts as itself, never attributed to the member who triggered it"* means
**installation token, never user-to-server.** Writes appear as `comrade[bot]`.
A user-to-server token would be easier (it inherits the member's push rights) and
would silently credit the agent's commits to a human — the invariant violated at
the auth layer.

This splits along the line the Python already draws: reads borrow the requester
(§4.1), writes act as the app.

### 16.4 Action tiering

⚠️ **Constrained by §13.** The agent never initiates outward communication that
reaches people who did not ask. GitHub comments are a different *surface* from
the group room but the same *shape*, so the ruling propagates.

| Action | Gate |
|---|---|
| create `comrade/*` branch, push commits | **none** — act, then diff card |
| open / update a **draft** PR | none — card |
| mark PR ready for review | card |
| comment on a PR or issue | **only when a human asked for it in-turn.** Never agent-initiated (§13) |
| request review from a person | as above |
| close / reopen someone's PR or issue | as above |
| merge, protected-branch write, force-push, secrets, Actions config | **capability never held** |
| repo events → group room | **never build** — explicitly excluded by §13 |

Repo activity reaches the team through the **wiki** — a compiled fact with a
`github` citation, surfacing as a diff card, which §13 classifies as a system
notice — never as the agent talking in the room.

### 16.5 Schema deltas

| Change | Why |
|---|---|
| `github_activity.node_type` — widen beyond `commit\|pr\|merge` to add `issue`, `review`, `comment`, `branch` | webhooks deliver all of these |
| `profiles.github_username` — exists, unused | the only path from `author_github` → `author_user_id` |
| new job type `ingest_github` | webhook → queue → `github_activity` |

Per the §8 migration policy: `CREATE INDEX CONCURRENTLY` on existing tables;
check constraints `NOT VALID` then `VALIDATE`.

### 16.6 New security surface

The webhook endpoint is **the first unauthenticated route in the codebase.**
HMAC signature verification is mandatory. Replay protection comes free by using
the delivery id as the existing `jobs.dedupe_key`.

**Injection surface expands.** `spotlight()` currently covers documents and chat.
PR bodies, issue text, review comments and code comments are attacker-controlled
on a public repo and head straight for the compiler. Spotlighting must extend to
the repo path **before** any repo-reading tool ships.

Feed the compiler PR titles, descriptions and review comments. **Not raw diffs** —
the compiler would emit noise facts.

### 16.7 Not building

Issue-to-PR autonomous coding (Copilot runs it inside Actions behind three
mandatory security scans; not winnable solo) and code review (CodeRabbit,
Graphite and Copilot own it).

---

## 17. The execution sandbox

**Owner decision, 2026-08-18:** a team-shared sandbox is acceptable; loss of
agentic capability is not.

### 17.1 The shape

**One persistent, shared sandbox per team.** The ADK agent stays on Comrade's
server and calls into it over RPC — LangChain calls this *"sandbox as a tool"*
(agent outside, dangerous operations inside), and it is the pattern Comrade
already is, so the agent needs no restructuring.

| Rejected | Why |
|---|---|
| **Local execution per developer** | Knowledge mismatch. Two members get different answers to the same question with no way to tell which half was local-only; ambient capture degrades into manual publishing; three of the four AI pillars need a server — the `idle` nudge must fire *for the person whose laptop is closed*. Decisive: unknowingly-duplicated work is the exact coordination gap Comrade exists to surface |
| **Ephemeral per-turn sandboxes** | Shared warm state (installed deps, hot build cache) is a feature. Cold start stops mattering when the box persists |
| **Step-level isolation** (Tines 3B: *"every step functions as a computer"*) | Correct for connector workflows, destroys a coding harness — write → test → read → edit needs continuous state |
| **Bare containers** | Largest attack surface (shared kernel syscalls); a Feb 2026 comparative study (arXiv 2606.08433) says avoid for untrusted code |
| **Codex's network-off agent phase** | Right for running strangers' code at OpenAI's scale, wrong for a dev harness that must `npm install` mid-task |

### 17.2 Capability audit

| | Shared team sandbox |
|---|---|
| Full shell, root | ✔ — more than Claude Code gets on a Mac |
| Read/write any file; install, build, test | ✔ |
| Network | ✔ (proxied — see 17.3) |
| Long-running dev server | ✔ **and better** — persists, gets a shareable URL |
| State across turns | ✔ **and better** — survives a laptop closing |
| Read the developer's own machine (`~/.ssh`, dotfiles) | ✘ |
| Drive the developer's locally-running app | ✘ |

Two losses, both at the laptop boundary, neither of which is team context.
**Net: no meaningful capability loss.**

### 17.3 Transparent-proxy credential injection

**Supersedes the earlier "diff out, executor pushes" proposal.**

The sandbox holds **no credentials, ever**. Egress passes through a proxy that
recognises the destination and injects the real token at the network layer; the
agent holds only a synthetic credential meaningful to the proxy.

Two independent sources: LangChain's sandbox write-up (*"the agent never holds
the credential"*) and Tines 3B shipping it (*"credentials injected via a
transparent proxy; secrets never visible to the builder, the AI, or stored in the
code"*).

Why it beats diff-out: diff-out works for GitHub **and only GitHub** — every new
connector would need its own executor. The proxy generalises to anything and
preserves the agent's ability to call APIs directly, which is the capability the
owner refused to trade.

**The cautionary case:** Cursor's background agents run with git credentials in
the box, and the agent has filesystem read on the home directory where
`~/.npmrc`, `~/.docker/config.json`, `~/.ssh` and `~/.gitconfig` live. A prompt
injection does not need to escape the sandbox — it reads the key out of its own
environment. Copilot mitigates differently: its token *is* in the runner, but
scoped so tightly (`copilot/*` only) that blast radius is small.

Tokens are the **team's GitHub App installation token, never a member's personal
token** (§16.3).

### 17.4 Private threads must never enter the shared sandbox

The sandbox is team-scoped with **no RLS** — file reads are not DB reads, so
anything written there is readable by any member's turn. If a private-thread turn
writes context, scratch files or a transcript into the shared workspace, the
hardest product invariant leaks through the filesystem, silently, below the layer
that enforces it.

**Rule: only team-scoped content enters the shared sandbox.** No private-thread
context, no per-member credentials, no personal tokens. If private turns need
execution at all, they get separate scratch space.

Free to design in now; very ugly to discover later.

### 17.5 Concurrency

**Git worktrees** — one shared sandbox, one worktree per active work item, shared
package cache underneath. Solves Maya-edits-while-Sam-tests without separate
boxes. Precedent: Domenic Denicola's agentic-coding setup uses worktrees for
exactly this (parallel agents, one codebase).

This is the filesystem-level counterpart to the §4.3 advisory lock, which
operates at the DB level.

### 17.6 Isolation and vendor shape

Hypervisor-grade (Firecracker) **between teams** — the multi-tenancy boundary,
and the only place the container-vs-microVM argument still applies. **Within a
team: none.** A shared filesystem is the point.

The persistent-per-team model favours **persistent VMs over ephemeral microVMs**:
E2B's 78ms cold start (p50, Jan 2026) stops being the deciding feature.
Per-second persistent VMs (Box, exe.dev) fit better. **Buy, don't build.**

Egress hardening regardless of allowlist: block `169.254.169.254` (cloud
metadata) and RFC1918.

### 17.7 Drift

A long-lived team box accumulates stale branches, orphaned processes and mutated
global state. Answer, per Cursor's cloud-agent-environment writeup: the sandbox is
**reconstructible from a declared spec** (Dockerfile / devcontainer) plus a reset
path. Their "Cloud Doctor" self-healing pattern is the mature version.

### 17.8 Promoted to prerequisite

- **Job-backed turns.** `/agent/turn` blocks a threadpool worker synchronously; a
  harness turn runs for minutes. The synchronous model does not bend here, it
  breaks.
- **Connection pool** (§3.2) — long turns make it materially worse.

### 17.9 Open

1. **Scoping unit** — per team, or per project/repo? A team with three repos may
   want three boxes. Tines 3B's *Spaces* primitive (connectors + skills +
   permissions scoped together) raises this; unsettled.
2. **Where the proxy lives** — its own service, or part of the executor path.
3. **Environment spec** — agreed in principle (17.7), not designed.

---

## 18. Comparison sources

| Source | Verdict |
|---|---|
| **Delta / DeltaDB (Zed)**, launched 2026-08-12 | Adjacent, and a close neighbour once §14 applies. CRDT operation-level replication of worktree + conversation; comments anchored to code that survive revision; the agent is a thread participant you interrogate (*"you don't reconstruct intent from a diff — you ask the agent to explain it"*). **Take:** anchored comments that survive revision — Comrade has nothing, `memory_reverts` is a binary undo with no voice, and this is also the mechanism for the §6.3-6 conflict-state gap; plus "ask the agent why" on any artifact. **Don't take:** the CRDT (reading a repo is not syncing a worktree) or "never summarise" (Comrade compiles because something has to read the result) |
| **Tines 3B** (crawled 2026-07-15) | **Take:** transparent-proxy credentials (§17.3); Autofix/Autotune — the platform proposes its own reliability and clarity improvements onto a **reviewable branch authored by the bot**, listed beside human authors, which is the shape of Comrade's missing proactive layer gated by branch rather than dialog; spend monitoring by team *and* model with cache-hit rate, giving the §3.1 token-columns gap a target shape. **Don't take:** step-level isolation (§17.1), ROI dashboards, the 313-example gallery |
| **Copilot coding agent / Devin / Codex cloud / Cursor** | Pattern A (§16.1); Cursor is the credential-leak cautionary case (§17.3) |
| **Linear** | Pattern B (§16.1). Their stated thesis matches the owner's: *as code generation gets cheaper, the bottleneck moves to coordination, verification and context* |

**Signal worth recording, not a task:** Delta connects to Claude Code, Linear
delegates to Copilot/Devin/Charlie/Cyrus, and Tines 3B runs Claude Code and Codex
inside its governed environment. Three platforms independently concluded that
hosting *other people's* agents is table stakes. Comrade's own agent stays primary
for v1 (§14) — but three companies arriving there separately is worth writing
down.

---

## 19. Revised order

Supersedes §11. Ordered by what unblocks what.

1. **§2.1 — reads run as the requester.** Before any message- or repo-reading
   tool. Deletes code.
2. **§10 + §13 — remove T3/two-key and `team_propose_group_message`.** Both are
   deletions, both touch the same three test files, and both change the consent
   shape the gate is written against. Do them together, before item 3.
3. **§15.4 + §15.5 — the tool registry and the chokepoint**, scoped to
   `outbound`/`db` surfaces. Unknown tool fails closed.
4. **§2.2 — unique constraint on `action_hash`.** One line.
5. **§2.3 — write page descriptions.** Unblocks the recall design already built.
6. **Connection pool** (§3.2, §17.8).
7. **§16.5 + §16.6 — GitHub ingestion.** Webhooks → `github_activity` → compile
   into the wiki with `github` citations. **No sandbox required**, which is the
   point: this is the differentiator (team memory from real repo activity) and it
   ships independently of the harness half. If §17 slips, Comrade is still a
   working product.
8. **§9.3 G3 — rejections reach the model.** Highest product value per line;
   today the agent cannot learn inside a session.
9. **Job-backed turns** (§17.8) — prerequisite for anything sandboxed.
10. **`agent_steps` table.** Kills the jsonb rewrite, the per-step connection and
    the `agent_runs` bloat; unblocks `parent_run_id`.
11. **§17 — the sandbox**, and the six harness tools (§15.3).
12. **Session storage + `RunConfig`.** Conversation memory and turn caps; needs
    the dedicated schema for ADK's tables.
13. **Cheap columns migration** — tokens/cost on `agent_runs` (§18, Tines shape);
    `batch_id` and `agent_run_id` on `consent_queue`; `cancelled` status.
14. **Fix the chat sweep query.**
15. **`messages_search` + `document_read` + repo read tools.** Requires
    `documents.parsed_text` and a `tsvector` index.
16. **Room advisory lock** (§4.3) + worktree allocation (§17.5).
17. **Anchored comments on memory entries** (§18, Delta) — also the §6.3-6
    conflict-state mechanism.
18. **§9.3 G4/G5 — standing approvals + circuit breaker.** The earned-trust
    ratchet; needs a proactive producer to exist first (§12).
19. **`scope` column on `memory_pages`** (§4.5) + `skill` page kind (§6.2).
20. **Consolidation context cap** (§6.2 item 3).

Deferred, unchanged: `idx_messages_private_owner` sort column, autovacuum tuning,
uuidv7 for new tables, hybrid retrieval, cross-page links, rot detection,
partitioning, G6 (policy re-check), G8 (racing approvers), G9 (allow-rule
sanitisation).

---

## 20. Memory shapes — three external systems assessed (appended 2026-08-18)

Two articles and one shipped product, reviewed against the pipeline as built. The
first article is advocacy; the second is measured; they disagree, and the disagreement
is the useful part. Warp (§20.7) was added the same day and is the only *team-scoped*
comparison available — everything else in §20 and §6.2 describes single-user memory.

| Source | Kind | Verdict |
|---|---|---|
| **Daily Dose of DS — "Agent memory is only as good as its schema"** (fetched 2026-08-18) | Vendor-adjacent walkthrough of Graphiti/Zep typed-graph memory. No benchmarks. | Advocates the typed-entity/edge graph Comrade parked on 2026-07-15. One reusable idea (context templates), one reusable constraint (10/10/10). |
| **pinglin.tw — "The shapes of agent memory"** (fetched 2026-08-18) | Empirical, benchmarked (LongMemEval-S/M, LoCoMo), with confidence intervals and cost-per-correct-answer. | The stronger source. Measures the graph the first article sells, and it loses. Directly challenges Comrade's live recall path (§20.4). |
| **Warp Agent Memory** (docs.warp.dev, fetched 2026-08-18) | Vendor documentation, research preview. No benchmarks, no implementation detail. | Design shape only, not evidence. The closest comparable — shipped, team-scoped. One new item (§20.7.1), one validation, one third-confirmation. |

### 20.1 The taxonomy, and where Comrade actually sits

The second article defines three shapes:

- **File-based** — model curates markdown; a small index loaded each session, topic
  files reached by grep. Claude Code, Cursor, Cline.
- **Structured** — every interaction auto-extracted to atomic facts, embedded without
  LLM reasoning, recalled by ranked retrieval. mem0, Letta, Zep.
- **Experience-based** — an RL-trained policy learns *how* to use retrieved episodes,
  not just what to retrieve. MemHarness.

**Comrade resembles file-based and is not file-based.** The article's file-based losses
come from *index + grep retrieval failing to find scattered facts* — not from markdown.
Comrade performs no retrieval at all: `all_active_pages()` returns the entire active
wiki, and consolidation reads all of it. There is no index to be bounded by and no
ranking to miss a fact.

At pilot corpus size this is strictly better than both measured shapes. Comrade is not
winning an architecture argument here; it is small enough to skip the problem.

### 20.2 What the measurements validate

**Raw dated facts beat distilled graphs, head to head, at a fraction of the cost.**
Entity-and-time graph ingestion measured at ~$14 per long user history against ~$0.03
for embedder-only writes — and the graph still lost on accuracy. In the store-only
comparison the hosted graph system scored 0.75 while handing the reader 6x the context.

This retires the open question in `project_comrade_memory_ingestion` ("graph-over-vectors
parked; revisit on measured multi-hop failure"). The revisit trigger stands, but the
prior has moved: the graph is now the *expensive* option that underperforms, not the
sophisticated option deferred for cost. **Decision: graph stays parked, now on evidence
rather than intuition.** The first article's 10/10/10 constraint (10 entity types,
10 edge types, 10 fields each, as ceilings) is recorded as the design cap *if* that day
ever comes.

**Bi-temporal validity windows are the one durable win of the entity lineage.** The
article credits the graph camp with exactly one thing that transfers: new facts close
old ones rather than competing with them in a ranking. Comrade already does this —
`is_active`, `valid_from`, `valid_until`, and the never-active tombstone (§6.0). The
expensive part (LLM entity resolution per message) is the part Comrade skipped.

**Consolidation's null result does not apply to Comrade.** The article measures
background "dreaming" — dedup and clustering — and finds it "merged real duplicates and
bought no accuracy at the scale tested." Comrade's stage 2 is not an accuracy play: it
is what keeps the wiki small enough to fit whole-in-context. Different mechanism, same
name. No change.

### 20.3 What the measurements challenge — three findings

**20.3.1 🔴 Temporal metadata is discarded at render. Measured cost: 39 points.**

The widest single gap in the benchmark is temporal reasoning: **80% vs 41%**
(multi-session joins second at 61% vs 33%). The dividing factor is whether facts reach
the reader carrying dates.

Comrade *stores* the temporal data — `memory_versions.valid_from`, `created_at`,
`change_type`, and `memory_citations.source_kind` — and then throws it away at the
boundary. `wiki.py:76`:

```python
bullets = "\n".join(f"- {f['text']}" for f in p["facts"])
```

Every fact reaches the agent as an undated bullet with no provenance. A fact compiled
this morning and one compiled in May are indistinguishable. This directly undercuts the
standing "memory-as-hint — verify action-bearing facts against live state" invariant:
the agent cannot prioritise what to re-verify when every line looks equally fresh.

The fix is annotation at projection time — `as of <valid_from>`, plus source kind
(`from chat` / `from doc`) — in `render_team_wiki()` and in
`build_consolidation_prompt()`. No schema change; the columns exist and
`all_active_pages()` already joins the table they live on.

**Decision: do this first.** It is the cheapest item in §6.3-adjacent work and now has
the largest measured effect of anything in either article.

**20.3.2 🔴 Extractor starvation is Comrade's unguarded failure mode.**

Named failure: a graph or extraction store "can only answer from what the extractor
wrote down" — a weak extractor starves the pipeline regardless of reader quality.

Comrade is maximally exposed. Stage 1 is a single LLM call and the *only* path from
source to memory. There is no vector fallback and no raw-text search (the RAG deletion
on 2026-07-15 removed the net; `documents.parsed_text` + `tsvector` is listed at §19-15
but unbuilt). If extraction misses a fact, that fact is unreachable forever — no
degraded retrieval path exists behind it.

This is a larger practical risk than any retrieval-architecture question, and **stage-1
recall is currently unmeasured.** No metric, no eval set, no regression signal.

**Decision: add stage-1 extraction recall as a pilot metric**, alongside the existing
"context updates/day". A held-out set of documents with hand-labelled expected facts is
sufficient; this does not need a framework.

**20.3.3 🟠 Consolidation cost — the article's guidance points at Comrade's design.**

Explicit guidance: *"avoid LLM-per-message extraction unless entity aggregation is
irreplaceable."* Comrade runs two LLM calls per compile, and stage 2 sends the **whole
wiki** every time. Cost scales as O(wiki size × compile frequency), and the only
throttle is the >=5-message debounce.

At 300 facts this is fine and correct. At 3,000, every fifth message re-sends 3,000
facts through a Pro-class model. This is §6.3-8 (unbounded consolidation cost) and
§19-20 (consolidation context cap), now with an external cost argument behind it. No
new task — it raises the priority of one already recorded.

### 20.4 🔴 Recall already uses the index-and-select shape — section corrected 2026-08-18

**Correction.** As first written this section claimed the page selector was an unbuilt
future plan to be avoided. It is shipped and live. The decisions below replace the
originals; the reasoning that changed is stated rather than silently dropped.

`page_index()` the function is unused (§2.4, §6.3-10) — but `agent/agent.py:57`
(`wiki_section`) builds the same projection inline, titles + descriptions, and injects it
into the system prompt on **every turn**. The agent then pulls page bodies on demand via
`memory_read_page` (`agent/tools.py:130`). The docstring states the intent outright:
*"Claude Code's model: the index is always in context, page bodies load on demand."*

So Comrade is a hybrid neither article names:

| Path | Shape | Cost / failure driver |
|---|---|---|
| Write (consolidation) | whole corpus in context, no selection | corpus size |
| Read (agent recall) | index in prompt, select one page | fact scatter |

The benchmark critique — file-based 44.9% against structured 73.6%, widest on
multi-session joins (33% vs 61%) — lands on the read path **now**, not at some future
scale.

**Three things make Comrade's version stronger than the systems that scored 44.9%:**

1. **The index is complete.** Every page with active facts appears. The article's
   *index-bounded retrieval* failure describes a small curated index dropping facts
   before write time; Comrade drops nothing.
2. **`memory_read_page` is callable repeatedly.** Multi-hop is possible, not blocked —
   the agent has to *decide* to open a second page, but nothing prevents it.
3. **Facts return with their citations**, so the reader gets provenance rather than
   opaque text.

The residual failure is therefore narrower than the benchmark's: not *"the index was too
small"* but **"the agent opened one page when the answer needed two."**

**One aggravating factor:** page descriptions are unwritten (§2.3), so the live index is
titles only. The selector is running on the weakest input it could have.

**Decisions (revised):**

1. **Page descriptions promoted, not merely retained.** They are not routing polish —
   they are the input the live recall path selects on. §19-5 keeps its slot, with its
   rationale corrected in the *opposite* direction from what this section first said.
2. **The selector is kept and augmented, not demoted.** "Demote to one candidate among
   others" was written believing the mechanism was unbuilt; demoting a shipped path is a
   migration, not a plan change. Index and ranked search fail differently and cover each
   other — Claude Code, Warp (§20.7.2) and the article's own hybrid all ship both.
3. **Add `memory_search(query)` beside `memory_read_page`.** Ranked over
   `memory_versions.fact where is_active`, returning facts with citations and dates,
   capped at N. Backing: `tsvector` + `ts_rank_cd`. `pgvector` + RRF fusion is a
   *separate, later* decision taken only if lexical alone measurably misses. All native
   Postgres, inside RLS, inside the existing transaction (§6.2-4).
4. **The trigger splits in two.** The original attached a read-path replacement to a
   write-path alarm:

   | Path | Trigger | Response |
   |---|---|---|
   | Read | measured cross-page recall failure — answer needs two pages, agent opens one | `memory_search` (decision 3) |
   | Write | consolidation prompt size | context cap (§19-20, §6.3-8) |

   Corpus size and fact scatter are independent pressures arriving at different times for
   different reasons. One alarm cannot cover both.

**Unchanged from the original section.** The RAG distinction still holds: what was
deleted on 2026-07-15 was **RAG as the primary memory model over raw document chunks**;
`memory_search` is **ranked retrieval over already-compiled, cited, bi-temporal facts**.
Different input, different role, different failure mode. The deletion is not reversed.

Also unchanged: the article's associative-expansion layer (co-occurrence over ranked
results) showed **no measured improvement** — the author flags it as "design capability,
not a measured contributor." Do not build that part.

### 20.5 Methodological caveat

Recorded because it bounds every number above: swapping the reader-and-judge stack moved
a score by **6.9 points on byte-identical retrieval** — larger than the gaps between
stores that tie. On LoCoMo the evaluation protocol dominated the architecture choice; on
LongMemEval-M the same store pair separated by 15 points. The author's conclusion:
*"No single benchmark ranks memory systems."*

Treat §20.3 and §20.4 as direction and rank ordering, not as calibrated effect sizes.
The 39-point temporal gap is the most robust of them (largest, and mechanically
explicable); the rest are directional.

### 20.6 Net effect on §6.3 gaps

| §6.3 gap | Change |
|---|---|
| 4 — no scopes | Third independent confirmation (§20.7.3). Still parked, shape more certain |
| 8 — unbounded consolidation cost | Priority raised (§20.3.3); write-path trigger named separately (§20.4-4) |
| 10 — `page_index()` dead code | **Reframed.** The *function* is unused, but its projection is live in `agent.py` — not dead architecture to close, shipped architecture to strengthen (§20.4) |
| — | **New:** temporal/source metadata discarded at render (§20.3.1) |
| — | **New:** stage-1 extraction recall unmeasured (§20.3.2) |
| — | **New:** no ranked search beside the page selector (§20.4-3) |
| — | **New:** no explicit member-initiated memory write (§20.7.1) |

Unchanged by these sources: skills / procedural memory, cross-page links, conflict state,
staleness detection, bootstrap. The L3 persona layer and scopes are unchanged as *tasks*
but now carry three independent confirmations of their shape (§20.7.3).

### 20.7 Warp Agent Memory (fetched 2026-08-18)

Source: `docs.warp.dev/agents/agent-memory`. **Vendor documentation, research preview, no
benchmarks and no implementation detail** — same epistemic standing as §6.1's PromptQL
crawl. Useful for design shape, not as evidence.

The most directly comparable system reviewed to date: a shipped product doing
*team-scoped* agent memory. Everything else in §20 and §6.2 describes single-user memory.

**Their design.** Three stores by ownership (Personal / Agent / Team). Two write paths —
automatic extraction when a conversation ends, plus an explicit "remember this"
mid-conversation. Updates "resolve contradictions with prior memories". Retrieval searches
the accessible stores at task start and injects, with further on-demand pulls
mid-conversation. Source recorded, changes tracked.

**20.7.1 New — explicit "remember this". Comrade has no member-initiated write at all.**

Every Comrade fact arrives through automatic extraction. A member who watches the compiler
miss something important has no recourse.

Worth building because:

- It is the **highest-signal fact in the system** — a human explicitly marked it. Better
  evidence than any extractor heuristic.
- It is the **only available mitigation for extractor starvation** (§20.3.2). Stage 1 is
  otherwise the sole path from source to memory, with no degraded fallback behind it.

**The sole-writer constraint improves the feature rather than blocking it.** Members
cannot write `memory_*` — RLS grants that to `comrade_pipeline` alone (§6.0). So
"remember X" enqueues a compile job carrying that text as a single spotlighted candidate:
it still runs extract → consolidate → apply, still earns a citation, a diff card and a
revert. A system that let members write memory directly would open an unspotlighted
injection path straight into agent context. Routing through the compiler costs one job
enqueue and keeps the datamarking guarantee.

**Decision: accept, queued after the three finishing items in §21.** New surface, and the
queued work is all completing what already exists.

**20.7.2 Validation — two retrieval modes, shipped.**

Warp injects relevant memories at task start *and* allows mid-conversation requests. That
is §20.4 decisions 2 and 3: index plus search, neither replacing the other. Third
independent system with this shape (Claude Code, Warp, the article's own hybrid). No new
work; it raises confidence in the §20.4 correction.

**20.7.3 Third confirmation — scopes by ownership.**

Personal / Agent / Team. §6.3-4 (no scopes) and §6.2-1 (L3 persona layer) now carry three
independent confirmations: PromptQL's four scopes, TencentDB's Private/Team/Restricted,
Warp's three.

Tension worth recording: Comrade's positioning thesis is flat, visible, shared context
(§14), and a *personal* store cuts against it. The precedent already exists though —
private threads never reach memory (§6.0) — so a personal store is the L3 layer scoped to
one member, which is exactly where §6.2-1 landed. No change to the parked item; more
confidence in its shape.

**20.7.4 Rejected — the write trigger.**

Warp writes when a conversation ends. A Comrade group room has no conversation boundary;
it is continuous. The watermark + ≥5-message debounce (`chat.py:31`) is the correct
adaptation for a room that never ends. Do not copy.

**20.7.5 Where Comrade is ahead.**

Warp's docs claim traceability and auditability. They describe no injection defense, no
enforced sole-writer, and no revert. Comrade has DB-trigger-enforced citations,
spotlighting before every LLM call, a dedicated writer role, and member-insertable reverts
(§6.0). Nothing here to adopt.

One claim recorded as false as stated: automatic extraction "runs asynchronously
post-conversation without consuming tokens." It consumes tokens — it does not consume
*turn latency*. Comrade's worker jobs already have the real property.

---

## 21. Order amendment (appended 2026-08-18, revised same day)

Amends §19. Only the memory items move; everything else keeps its §19 position.

**Queued, in order:**

1. **Temporal + source annotation** in `render_team_wiki()` and
   `build_consolidation_prompt()` (§20.3.1). Insert at §19 position 5. Largest measured
   effect of anything in this document, ~3 lines, no migration.
2. **§19-5 page descriptions — promoted, not merely retained** (§20.4-1). Originally
   justified as unblocking a future selector. The selector is live and running on titles
   alone, so this feeds a shipped path rather than preparing an unbuilt one.
3. **Stage-1 extraction recall eval set** (§20.3.2). A measurement, not implementation
   work. It should exist before the pilot generates data worth reading.
4. **`memory_search(query)` beside `memory_read_page`** (§20.4-3). Trigger: measured
   cross-page recall failure, readable from item 3 plus `agent_runs`. Build once the
   measurement says it is needed — not before.
5. **Explicit "remember this" → compile job** (§20.7.1). New surface; after the finishing
   work above.

**Priority raised, position unchanged:** §19-20 consolidation context cap (§20.3.3) —
still after the sandbox work, because it is a cost ceiling rather than a correctness fix.
Its trigger is now named separately from the read path (§20.4-4).

**Deferred list, amended:**

- **Hybrid retrieval — split.** Lexical `memory_search` becomes item 4 above with a named
  trigger. `pgvector` + RRF fusion stays deferred as a separate later decision, taken only
  if lexical measurably misses (§20.4-3).
- **Cross-page links (wikilinks) — still deferred, but cheaper than recorded.** Stage 2
  already reads every page and already picks a target page per added fact, so emitting
  page→page links is near-zero marginal cost — the cheap half of a graph without the
  entity-resolution bill that made the typed graph lose (§20.2). Unproven either way: the
  article's co-occurrence expansion showed no gain, but authored links are a different
  signal and nobody measured those. Not worth a dedicated slice; worth doing if stage 2 is
  opened for another reason.
- **Scopes / L3 persona layer — unchanged as work**, three independent confirmations of
  the shape (§20.7.3).

**Graph / typed-entity memory: parked, evidence-backed** (§20.2). Trigger unchanged
(measured multi-hop failure), prior inverted.

---

## 22. External evidence — Linear's "How Teams Build" report

> Source: `linear.app/data`, read 2026-08-18. Linear's first annual report,
> thousands of teams, Jan–Jun 2026. **Caveat: first-party vendor data on a
> self-selected base** (teams already running Linear skew process-mature) and
> marketing-adjacent. Directional, not independent. Recorded because it is the
> largest published sample and it bears on two things already decided here.
>
> Numbered 22 because §20–§21 (memory shapes, and the order amendment they
> produced) were appended by a concurrent session on the same day. No overlap in
> subject; this section adds no roadmap items and does not touch the §21
> amendment.

### 20.1 The numbers

| Finding | Figure |
|---|---|
| Teams running coding agents, weekly PRs | **21 → 65** (3×) |
| Teams without, weekly PRs | 8 → 10 (flat) |
| PRs overall since 2024 | **+111%** |
| Issues authored by agents / MCP clients | **~2,400/wk vs ~2,500 from people** — from ~0 in mid-2024; crossover imminent |
| Non-engineers attaching PRs | PMs 3% → 10%; designers 1% → 8% |
| **Planning time** | **0-minute change** for engineering and product; +1 min/month elsewhere |

Linear's own inference: *"AI has so far changed how teams execute far more than
how they decide what to build."*

### 20.2 Positioning correction

The report's closing claim — *"teams are working more, not less"* — describes a
Jevons effect: efficiency raised total effort rather than creating slack.

**So "Comrade saves you time" is the wrong pitch.** The pitch is **coherence at
speed**: at 65 PRs/week, with half of issues machine-authored and PMs shipping
code, nobody holds the shared picture any more.

Three independent sources now name the same enemy: Tines 3B's *"wild code"*,
Delta's *"agents write code faster than teams can review it"*, and this report's
flat-planning-vs-3×-execution split. That gap is Comrade's territory, and this is
the first time it has been measured rather than asserted.

Segment note: adoption grew evenly from 1–50 through 1,001+ employees (+14–19
points). Company size is not the gate — consistent with §14.

### 20.3 The provenance rule now has evidence

The rule recorded this session — **compiled facts cite human-verified artifacts
(merged PR, review, human comment), never the agent's own claims about its own
work** — was a principle when written. At ~50% of issues already machine-authored
and climbing, it becomes load-bearing: a compiler ingesting issue and PR text is
already half-ingesting machine output. Without the rule the wiki compiles machine
text back into machine context at scale.

### 20.4 What they do not measure — and what that implies

No data on review cycles, time-to-triage, cycle time, agent-issue duplication, or
coordination overhead. PR volume tripled and the report offers **no visibility
into whether review or merge kept pace.**

Two implications:

1. The downstream bottleneck is real, unmeasured, and unclaimed.
2. **Comrade would have that instrumentation by construction** — `agent_runs`,
   `change_log`, `github_activity`, `contribution_v`, `memory_compilations`
   already record what this report cannot speak to. Not a v1 feature; a latent
   asset, and a plausible retention answer.

### 20.5 What this does not change

Nothing technical. No new roadmap items. Duplicate/overlap detection gains a
third independent signal (Tines ships it; §17.1's local-execution rejection
turned on it; this report's volume makes it inevitable) but remains deferred.

---

## 23. Decision: billing model — per-team subscription under flat authority

**Owner decision, 2026-08-18.** First commercial design in the project; nothing
about payment existed in the repo before it (verified by grep — the only prior
hits for "subscription" are Realtime). Full design:
`docs/superpowers/specs/2026-08-18-billing-model-design.md`. Recorded here for the
parts that touch the architecture.

### 23.1 The concern, and what resolved it

Raised as: *does the flat/no-manager model survive payment?* Someone's card gets
charged, and that someone looks like the admin the product is positioned against.

Resolved by separating two things that had been merged:

- **Flat authority over the agent** — nobody approves another member's actions,
  nobody configures permissions. This is the product.
- **Flat authority over the commercial relationship** — nobody's card is on file.
  Never claimed, and not what the moat rests on.

The competitive read (project memory, 2026-07) names the incumbent constraint as
*"paid seats, a billing owner, and a persistent admin role, **admin-configured
permissions**."* The load-bearing clause is the last one. A payer is not that.

**The invariant, which is testable rather than aspirational:**

> **Paying buys continued service and nothing else** — no visibility, no vote, no
> member removal, no consent override, no private-thread access.

It is an RLS question, and RLS cannot grant what no policy references.

### 23.2 The model

| | |
|---|---|
| Unit | **Per team**, not per seat. Stripe customer per `team_id`, never per person |
| Price shape | Flat monthly + usage allowance, **hard stop at the allowance** — no metered overage |
| Team size | **Bands**, not a hard cap |
| Who pays | **Any member** can attach or replace payment. No billing-owner role, no transfer flow |
| Free tier | Repo ingestion, wiki, tasks, room, consent, nudges — memory and coordination |
| Paid tier | The sandbox and the harness |

**Why not per-seat.** Of the five reasons per-seat won in B2B SaaS, three fail
here: value scales weakly across a 3–10 headcount range; expansion for Comrade
runs across *teams*, not seats within one; and — decisively — **marginal cost
tracks usage, not headcount**, so per-seat mis-prices in both directions on a
product with real COGS. A sixth reason is fatal: per-seat requires someone to
administer seats, and that person is an admin. Supporting evidence is already in
project memory — ClickUp Brain² at *"$9–28/user/mo **+ credits**"* is per-seat
kept for legibility with usage pricing bolted underneath because seats do not
cover AI cost.

**Why a hard stop rather than metered overage** (this changed during spec
self-review): overage bills a team more than it agreed to, and **in a flat team
nobody has authority to approve that charge** — there is no budget owner by
construction. Governance already treats any spend as Tier 3 with a hard floor. At
the allowance the harness pauses; any member can raise the band and it resumes.

### 23.3 Churn — the question that prompted this

| Event | Effect |
|---|---|
| Member invited / leaves | **Nothing**, within the band. No price change, no billing action, no approval |
| Band crossed | Price steps automatically; visible to all members |
| **The payer leaves** | Subscription belongs to the team and persists. Any remaining member attaches a new method — no transfer, no permission, no support ticket |
| Payment fails | Grace period, then downgrade to free. Memory, room and history retained |

Worth stating plainly: a billing owner leaving without transferring is the most
common billing failure in team SaaS and usually needs support intervention. Here
it is a non-event. **Flat authority is why the problem does not exist, not the
cause of it.**

### 23.4 Architectural consequences

1. **`comrade_agent` gets no grants on `subscriptions` — not even `select`.**
   Governance rules any spend as Tier 3, hard floor; the cleanest expression is
   that the agent has no billing capability to tier. Plan state is not agent
   context either. Mirrors the memory sole-writer pattern and should be asserted
   the same way (grant-level test).
2. **A second unauthenticated route.** The Stripe webhook joins the GitHub
   webhook (§16.6) outside the JWT boundary. Two is a pattern, not two one-offs:
   both need signature verification and replay protection via the event id as
   `jobs.dedupe_key`. Worth one shared helper rather than two implementations.
3. **The token/cost columns stop being housekeeping.** §3.1 flagged
   `agent_runs` as having no token or cost columns and §19-13 filed them as a
   cheap migration. They are now the **billing input**. Turn counts already exist
   (`_check_turn_budget`, `server/app.py:106`); sandbox hours are new and have no
   recorder yet.
4. **The free/paid line matches the build order already recorded.** §19 item 7
   (GitHub ingestion → wiki) needs no sandbox and *is* the free tier; §19 item 11
   (the sandbox) is the paid one. The expensive capability sits behind the
   paywall by construction rather than by policy, and failed payment degrades to
   a real product rather than a lockout.
5. **No card data touches Comrade.** Stripe Checkout and Customer Portal own
   capture and updates end to end.

### 23.5 Deliberately deferred

Price points, band sizes and allowance amounts — owner decision: settle the shape
now, the numbers when commercial pricing is taken up. Also out of scope: annual
billing, trials, tax, enterprise invoicing, team dissolution, and a
student/sponsored programme (the beachhead is served by the free tier).

**Open, and flagged because it is governance-shaped:** whether a team may opt
into metered overage. That setting is itself a standing spending authorisation —
the first in the product — so it would have to be a team decision under the
consent protocol, not one member's toggle.

**No roadmap items added.** Nothing here changes §19 sequencing; items 7, 11 and
13 acquire a second justification.

---

## 24. Loose ends from the 2026-08-18 session

Findings raised in-session that were never written down — either because the
conversation moved on, or because they were offered and not taken up. Recorded
so they are not lost. **None of these are decisions**; several are explicitly
speculative and marked as such.

### 24.1 The handoff reframe (unlogged source: Ravi Mehta / Matthew Mamet, "The case against the full-stack builder")

A PM essay, anecdote-driven, no data — an argument rather than evidence, unlike
§22. Its central claim is nonetheless the sharpest reframe of Comrade's premise
found so far.

**The claim:** what used to guarantee quality was never the manager — it was the
**handoff**. The PM needed a designer to make it usable; the designer needed an
engineer to make it work. Those dependencies were checks and balances that came
free. AI dissolved them, so polished-but-wrong work now circulates before anyone
with domain knowledge has seen it. Their case: a designer shipped AI-generated
wireframes containing hallucinated features, and *"the designer saw the
wireframes at the same time as everyone else."*

**Why it matters here:** Comrade is positioned as *"the AI teammate for teams
that don't have a manager."* This suggests the missing manager was never the
problem. The problem is that **nobody with the relevant expertise sees the work
before it circulates** — which is more universal and more defensible.

It also explains *why* the degenerate gate in §12.4 is hollow. The old handoff's
value was that **a different person with different knowledge** looked. Requester
self-approval reproduces the ceremony and none of the function.

**Consequence worth flagging: after §10, non-code actions have no forced second
pair of eyes at all.** T3's two-key rule was the one mechanism that recreated the
handoff. Removing it remains correct — but the reason is narrower than
"two-key was overhead." It is correct *because GitHub branch protection does it
better for code* (§16.2). Nothing covers the non-code case, and nothing currently
plans to.

### 24.2 Strengthens an already-deferred item

The essay's substitute for lost handoffs is shared standards — *"a standard is a
playbook everyone knows cold before they sit down."*

That is exactly what memory gap §6.3-3 says Comrade lacks: *"facts only — no
skills, no procedural memory."* A standard **is** procedural memory. This
promotes the `skill` page kind (§6.2, §19-19) from "optional layering" to "the
artifact that replaces the handoff." No change of position in the order; a
stronger justification for it.

### 24.3 Speculative, not recommended

Two ideas that follow from §24.1 but are not supported well enough to queue:

1. **Reviewer routing.** Comrade has no notion that "this change touches auth,
   and Maya has owned auth for six months." It is computable from
   `github_activity` + `contribution_v` — CODEOWNERS, derived rather than
   declared. Attractive, unproven, and it edges toward the person-comparison
   territory that ruling 6 in the governance memo restricts. Would need that
   boundary settled first.
2. **Task lifecycle models the old world.** `proposed → confirmed → in_progress
   → done` has no "should this ship" step. Both §22 and this essay describe the
   constraint moving from *what to start* to *what to let out* (their framing:
   prioritisation → curation). Noted only; one blog post and one vendor report
   are not grounds for a schema change.

### 24.4 A caveat on §23 worth recording against itself

§23.4-4 observes that the free/paid boundary lands exactly on the §19 build order
— item 7 free, item 11 paid. That was not reverse-engineered to fit; the cost
structure (fixed per-team sandbox vs zero-when-idle tokens) forced the line
independently.

**It should still be treated with suspicion.** "The plan I already had is also
the optimal pricing boundary" is the shape of a conclusion that flatters the
prior. Worth a deliberate re-check when pricing is actually taken up, rather than
inheriting it as settled.

### 24.5 Doc hygiene — gaps in this document

1. 🟡 **The roadmap now lives in three places.** §11 (superseded), §19 (current),
   §21 (amends §19's memory items only). A reader must reconcile three lists in a
   1,900-line document to know what is next. **Recommend consolidating §19 + §21
   into a single ordered list** and marking both sources superseded — not done
   here, because §21 is another session's work and merging it unasked would
   discard authorship context.
2. 🟡 **No convention for concurrent appends.** Two sessions both wrote a §20 on
   2026-08-18; the collision was caught by hand and resolved by renumbering to
   §22 (see that section's header note). Nothing prevents a recurrence. Cheapest
   fix: check `grep -n '^## '` immediately before appending, and put the section
   number in the commit message.
3. 🟢 **The document is approaching the size where it should split.** Sections
   1–8 are the original audit; 9–13 are permission/consent; 14–19 are the harness
   arc; 20–23 are memory, evidence and commercial. A split by theme would help,
   but renumbering cross-references (~40 of them) is the cost. Not now; noted
   for when it next becomes painful.

### 24.6 Stale records outside this document

| File | Problem |
|---|---|
| `README.md:3` | Opens *"An AI companion for student group projects."* Four sections out of date since §14 |
| Project memory, target entry | Still records the target as "any small team, students = pilot beachhead." §14 makes developer and engineering teams primary |
| `HANDOFF.md` §7 | Still documents the T3 two-key governance ruling removed by §10 |

All three are flagged in the relevant sections' removal manifests but none has
been corrected. They are the records a new session reads first, which makes them
the highest-leverage stale text in the repo.

---

## 25. Multi-agent: Grok Bot assessed, and what it changes

> Sources, read 2026-08-20: `x.ai/news/introducing-grok-bot`,
> `docs.x.ai/grok-bot/approvals-security-and-privacy`, VentureBeat, TechTimes,
> Composio, plus an 18-page third-party systems-engineering playbook
> ("2026 Working Note on GrokBot Systems Engineering Practice") supplied by the
> owner. Vendor claims are that vendor's claims; the playbook is one author's
> synthesis, not benchmarked. Prompted by the question of whether Comrade should
> adopt a Grok-Bot-style multi-agent model.

### 25.1 What Grok Bot actually is

Launched **2026-08-11**, beta. **$120/month**, bundled via SuperGrok / Cursor Pro
/ Cursor Teams. Desktop and iOS; enterprise on waitlist.

| Element | Detail |
|---|---|
| Team shape | **2–6 bots in a group chat**; they message each other directly, pass ownership, share context in threads |
| Orchestration | A **Chief of Staff** bot above named specialists (Inbox Manager, Calendar Scheduler, To-Do Organizer, recruiting, expenses, bug fixes). Directive: check whether another bot owns this, delegate first, only act directly if nothing fits |
| Execution | **One persistent cloud computer** — a managed Linux VM, bot runs non-root — with files, browser sessions and CLI credentials shared across the whole roster |
| Skills | Learned by demonstration; shown once, saved as a reusable method |
| Routines | Triggered execution, runs while the laptop is closed |

### 25.2 🔴 The structural finding

**Grok Bot is one human with many agents. It is not many humans with agents.**

The cloud computer is *"assigned to your user account"*; the roster is *"all of
your Bots"*; an xAI employee describes it as *"eight arms like an octopus."* It
is a personal org chart with exactly one boss.

Which makes the Chief-of-Staff hierarchy **free** for them — with one human,
"who does the Chief report to" never needs answering. With two humans it becomes
a governance question their architecture cannot pose.

| | Humans : Agents | Shared memory | Governance |
|---|---|---|---|
| **Grok Bot** | **1 : N** | Per-user | None needed — one boss |
| **Slack Code** (§26 context) | N : N | ❌ 90-day archive, deleted at 1 year | "A human signs off" |
| **Comrade** | N : N | ✅ compiled, cited, bi-temporal | Peer consent |

**The gap is multi-human multi-agent.** Grok Bot demonstrates the specialist
roster works and leaves it single-player; Slack makes it multi-human and leaves
out memory and governance. Nobody occupies the intersection. This is a more
precise statement of Comrade's position than "flat governance," which §26 shows
does not survive contact with a free Slack workspace.

### 25.3 The shared-computer credential finding — Comrade is ahead here

xAI's own documentation states: **do not use separate Bots as a security
boundary.** Reported consequences:

- Every credential on the shared machine is reachable by **every** bot,
  including bots created later.
- **Deleting a bot does not remove its files or signed-in browser sessions** —
  offboarding is not cleanup.
- No product-specific spend cap; audit view of bot actions listed as "coming"
  as of August 2026.
- No per-site scoping, no per-bot credential assignment, no fresh approval on
  privilege escalation.

This is the Cursor credential-leak pattern (§17.3) shipped as a *documented
design choice* by a well-resourced team.

**Comrade's existing decisions already answer it:**

| Their limitation | Comrade's answer |
|---|---|
| Credentials on the shared machine, reachable by all | §17.3 — **transparent proxy; the sandbox holds no credentials at all** |
| Deleting a bot leaves its sessions behind | §17.4 — only team-scoped content enters the shared sandbox; nothing member-private is ever there to strand |
| No spend cap | §23 — hard stop at the allowance, no metered overage |

**It also independently validates §17.1.** Two teams converged on *one shared
persistent VM* rather than ephemeral-per-agent sandboxes. That decision was made
2026-08-18 on cost and warm-state reasoning; this is external confirmation of the
shape, with a worked example of the failure mode to avoid.

### 25.4 The playbook's maturity ladder, and where Comrade sits

The supplied playbook grades systems L0–L5: Chat → Role → Skill → Routine → Team
→ **Governed**. Its six invariants per workflow: one current owner, explicit
state, durable artifact, observable evidence, bounded retry, clear approval
boundary. Its best line: *"If any invariant is missing, the human quietly becomes
the memory layer or recovery system."*

**Slack Code ships L0–L1. This playbook describes L4–L5. Comrade has more of L5
than Slack does:**

| Invariant | Comrade |
|---|---|
| Compact ledger | `agent_runs` — flat, no `parent_run_id` |
| Observable state | `change_log` |
| Approval as policy, not mood | the consent tier table |
| Bounded retry | `jobs` — attempts, lease, dedupe |
| Evidence pointers | `memory_citations` |
| Untrusted content handling | `spotlight()` |
| **Independent verifier** | **nothing** |
| **Typed handoffs** | nothing (single agent) |
| **Workflow-spanning task id** | nothing — `agent_runs` is per *turn* |

### 25.5 The verification gap — three sources, one hole

The playbook's **evidence ladder** runs L0 *"Bot says it is done — never
sufficient"* to L5 *"independent verifier pass — autonomy gate,"* and insists on
**separating producer from verifier** because the maker is biased.

Three independent sources within three days name the same hole:

1. **§22 (Linear)** — PR volume tripled; they publish no data on whether review,
   merge or cycle time kept pace. Numerator without denominator.
2. **§24.1 (the handoff essay)** — AI dissolved the cross-discipline check that
   caught bad work before it circulated.
3. **This playbook** — "Bot says it is done" is level 0, and it is what every
   shipped product currently does, Slack Code included.

**Verification is the unsolved part of agentic team work.** Comrade sits at L0 on
that ladder: the agent produces a reply or proposal and nothing checks it.
Recorded as a finding, not a thesis — §24.5's warning about thrash applies.

Worth noting for later: Comrade's role-split (separate DB principals for propose
and execute) is unusually well shaped for producer/verifier separation.

### 25.6 Correction to §26's advice

§26 speculated that the harness might be the commodity half and that §23 could be
pricing it backwards. **Grok Bot charging $120/month for persistent agents with a
computer argues otherwise** — an order of magnitude above the $9–28/user
incumbents. Execution is not commodity yet. That was an over-correction made in
the immediate aftermath of the Slack launch; §23's free/paid line stands
unchanged pending real pricing work.

### 25.7 Decision

**Do not build multi-agent now.** The playbook's own decision framework says
*"one bounded task → one Bot + Skill; do not add yet: Manager or team,"* and its
30-day plan places a manager at Week 3, *"only when routing or coordination is a
demonstrated bottleneck."* Comrade has a single agent that cannot read chat,
cannot read a repo, and has no verifier. Specialists now would be L4 built on an
incomplete L1.

**Also: a Chief-of-Staff orchestrator is a hierarchy.** Adopting it wholesale
would replace the peer-consent thesis rather than extend it. Any future
multi-agent work must answer "whose authority does the orchestrator carry" —
which under §4.1 means the requester's, not its own.

**One cheap decision to take now:** add **`agent_runs.parent_run_id`** during the
`agent_steps` migration (§19 item 10). Already flagged in §3.1 as missing and as
the thing blocking subagents. Near-zero cost inside a migration that is happening
anyway; genuinely painful to retrofit. This keeps the multi-agent door open
without walking through it.

**No other roadmap change.** §19 items 1–7 are unaffected.

---

## 26. Phase 0 executed — corrections to this document

> Appended 2026-08-29 after executing Phase 0 of
> `docs/superpowers/plans/2026-08-29-comrade-v2.md`. Section number checked
> against `grep -n '^## '` immediately before appending, per §24.5-2.

### 26.1 🔴 New finding: the agent's group reply was blocked by RLS

`ag_messages_insert` (`20260612120000_action_consent.sql:33`) carried
`with check (... and thread_type = 'private' and sender_kind = 'ai')`, but
`server/app.py:_persist_ai_reply` inserts `thread_type='group'` on a group turn
under `Role.AGENT`. The reply to an `@comrade` invocation — one of the two
group-visible AI writes §13.1 explicitly keeps — therefore **failed in
production**. Verified against the running database, not inferred from the
migration text.

Nothing caught it: `tests/test_server.py` and `tests/test_server_stream.py`
both monkeypatch `_persist_ai_reply`, and `tests/test_runtime_live.py` calls
`run_turn` directly rather than the endpoint. No test ever asked the database
to accept an AI group message.

**Corrects §13.6.** That section states `_persist_ai_reply` inserts group
messages "ungated". True at the GRANT level, false at the POLICY level. The
policy now checks `sender_kind = 'ai'` only — the invariant it exists to
protect is that the agent never writes a message attributed to a human. Thread
type was never the guard.

Generalisable tell: a mock whose boundary is a single DB write removes the only
risk the thing it stands in for actually carries.

### 26.2 🔴 The obvious cross-tenant test for a connection pool proves nothing

Recorded because it will recur wherever pooling meets `SET LOCAL`.

The intuitive guard for §3.2's pool is: alternate TEAM_A/TEAM_B across many
borrows and assert the scope follows the borrower. **It stays green under a
mutation that makes `app.current_team_id` session-scoped** — confirmed by
running it. Every scoped borrow overwrites the setting with the correct value,
so the leak is invisible from inside a scoped borrow.

The leak is only observable from a borrow that sets **no scope of its own**.
`connect(Role.AGENT)` shares a pool with `team_session(Role.AGENT, …)`, so a
bare borrow after a scoped one exposes a session-scoped setting immediately.
Both guards are now mutation-verified in `tests/test_db_pool.py`.

A second wrong assertion, also removed: pinning a specific `pg_backend_pid`
across borrows. A pool gives no such guarantee — connections return
asynchronously and the pool had already grown to two — so that was a
scheduling coincidence, not a contract.

### 26.3 §2.1's fix removed a read surface the writes depended on

`INSERT … RETURNING` requires `SELECT` on the table. Revoking the agent's
`SELECT` on `messages` and `consent_queue` therefore broke
`_persist_ai_reply` and `propose_action`, neither of which §4.1 anticipated.

Granting a narrow `SELECT` back would have re-opened exactly the enumeration
§2.1 closes. Both writers now generate the row id in Python and drop
`RETURNING`, leaving **zero** agent read surface on those tables. The agent's
granted surface went from 21 tables to five.

### 26.4 §24.6's own record was already stale

§24.6 lists `README.md:3` as opening *"An AI companion for student group
projects."* It did not — it had already been updated to *"small teams working
without a manager"* before this session. The §14 correction (developer and
engineering teams primary) was still needed and has landed. Noted because a
stale-records list going stale is the failure mode §24.5 warns about.

### 26.5 The roadmap now lives in one place

§24.5-1 recommended consolidating §19 and §21. Done, in
`docs/superpowers/plans/2026-08-29-comrade-v2.md`. §8, §11, §19 and §21 are
superseded as sequencing authority and retained for their reasoning.

### 26.6 Decisions settled

- **§10's open question:** `tier` survives, narrowed to `('T0','T1','T2')`.
- **§7:** ADK session granularity is one per `(team, thread_type, thread_owner)` — the boundary is the person, not the channel (§4.2). Lands in Phase 1.
- **§7:** a queued member sees an honest line, not a spinner.
- **§13.4:** the private-thread publisher targets the current team only in v2.
- **§23.5:** no metered overage in v2.

### 26.7 Still uncovered, deliberately

After §10, **non-code actions have no forced second pair of eyes at all**
(§24.1). The removal is correct because GitHub branch protection does it better
for code (§16.2) — but nothing covers the non-code case and nothing currently
plans to. Recorded here rather than left to be rediscovered.
