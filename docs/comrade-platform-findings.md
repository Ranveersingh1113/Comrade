# Comrade → Team-Native Agentic Platform — Findings

> Date: 2026-06-27. Produced from a full code audit of Comrade + 6 research lenses on how
> Slack / Jira / Linear / Notion / Cursor / Codex / Claude / Leverage build comparable systems.
> Status: **findings only — no code changes made.** Next focus chosen: the production agent runtime.

---

## Verdict

**Comrade's security and memory foundations are best-in-class — stronger than what most named
products ship — and they extend upward to the enterprise vision rather than blocking it.** The work
is not a rewrite. It is: (1) build the production agent runtime that does not exist yet, (2) evolve
the single synchronous agent into a real tool-loop, and (3) add four layers — a work-graph for
team↔team, per-team MCP connectors, a risk-tiered autonomy gate, and an ambient memory loop.

**Recommended scope stance: evolve in place.** The foundation is strong and re-tierable, the schema
extends additively, and a v2-from-scratch would discard the hardest-won part (the role-split security
model) for little gain. The pilot core is not even fully running yet (no production entrypoint), so
"evolve in place" and "finish the pilot core" are the same first step.

---

## Part 1 — Current-state audit (code-grounded)

### Six drifts between the story and the code

| # | Drift | Reality |
|---|---|---|
| 1 (biggest) | "HTTP-triggered, cron, document-action triggers" | **None exist.** The agent only runs via `InMemoryRunner` in tests/smoke scripts. No HTTP server, no scheduler, no event triggers. The "continuously monitoring" surface is unbuilt. |
| 2 | `agent_runs` for "crash recovery + eval" | **Never written.** No step log, no resumability, no observability. |
| 3 | Memory updates from chat | **Only documents ever compile to memory.** `memory_citations` allows `message`/`github` but only `document` is produced. Chat→memory has no code path. |
| 4 | Embeddings | Schema comment says `text-embedding-3-small`; code uses `gemini-embedding-001` MRL-truncated to 1536. Stale OpenAI key still in config. |
| 5 | "Four roles" | Three Postgres roles (`agent`/`pipeline`/`executor`) + built-in `authenticated`. `ADMIN` is the **postgres superuser URL**, not a least-privilege role — and `user_session` (human-approval path) runs through it. |
| 6 | Consent TTL | Code writes **7 days**; schema comment says "~5 min". |

Also: `embed`/`compile_memory` job types declared but never enqueued (compile runs inline); agent is hard-pinned to Flash (Pro escalation lives only in the compiler).

### Per-subsystem: strengths + what won't scale

- **RLS + 4-role + GUC multi-tenancy** — genuine defense-in-depth (GRANT capability × `current_team()` row scoping × `SECURITY DEFINER` helpers with empty search_path). **Risk:** `SET LOCAL app.current_team_id` + transaction-mode pooling (Supavisor/pgBouncer) leaks tenant context under load unless every access is txn-wrapped; connection-per-call (no pool); `user_session` on the superuser URL.
- **Agent core (1 agent, 4 tools, synchronous)** — clean server-bound `team_id`/`requester_id`; pure-vs-wrapper split is testable. **Risk:** no production entrypoint at all; `InMemoryRunner` ephemeral sessions; fixed 4-tool Flash agent can't host "connect any MCP / execute team actions"; `agent_runs` unused.
- **Consent / executor** — the crown jewel: structural role split, CAS exactly-once, hash + expiry + precheck re-verification. **Risk:** only 2 executors registered (every new action = new exec+precheck+enum); `execute_consent` runs inline (slow external calls block the approver); 7-day TTL; no rate limiting on proposals.
- **Memory / pipeline / embeddings** — compiled-artifact model with citations + bi-temporal versioning, sole-writer invariant at GRANT level, real spotlighting. **Risk:** synchronous embed inside the job handler; whole-document recompile only (no chat ingestion); no per-team vector partitioning; arbitrary 1536-dim MRL truncation locked into the column.
- **Job worker (polling)** — correct `FOR UPDATE SKIP LOCKED`, retry ×3. **Risk:** single-threaded, no daemon/backoff/visibility-timeout reaper (a crash mid-job strands it in `processing`); runs on the superuser connection; no dead-letter/alerting.
- **One-room-per-team** — **zero schema support** for team↔team chat or cross-team sharing; the GUC holds exactly one `current_team_id`. This is a foundational addition, not an extension.

---

## Part 2 — Product intent vs platform ambition

### Today's design (as built/specced)
Two-layer room (shared group room + private AI thread per member, invisible even to admins); tiered
consent (group posts / task creation / on-behalf messages need approval; private nudges / memory /
deadline tracking / summaries do not); compiled-artifact memory with citations + reverts; four AI
pillars (monitor communication, surface accountability gaps, engage with documents, take action);
assignee-confirmation invariant on tasks; ≤4-member student-pilot scope.

### The owner's scaling vision
Team↔team chat + resource sharing; ambient chat-monitoring to update project context; per-team MCP
connectors (GitHub/Notion/Drive/anything); agent executes team-given actions (Leverage / "Claude
tag" style); agent owns deadline + per-member task monitoring; "all of Claude/Codex/Cursor but for a
team." Directive: think production-wise, don't over-index on RLS/consent friction.

### Six tensions
1. **Single-room data model vs team↔team** — everything is scoped to one `team_id`; RLS-per-team is a wall, and the vision wants doors in it. New tenancy primitives needed.
2. **Consent-visible boundary vs autonomy** — consent gates "anything visible to others"; autonomous team agents need the boundary drawn at "risk/reversibility" instead. `reversible: bool` is the unused seed.
3. **Closed toolset vs arbitrary MCP** — 4 server-bound tools (cap 5–15) vs untrusted, dynamically-mounted third-party tools whose descriptions can be poisoned. Needs a per-connector trust + scoping tier.
4. **Compiler-only memory vs ambient chat-monitoring** — continuous chat→memory is the exact poisoned-memory path the design closed. Resolve via continuous-but-still-compiler-mediated ingestion ("never *directly* from chat").
5. **Single-call compiler cost** — feeding all active facts to Gemini in one call breaks at multi-team/multi-month scale (docs already name Graphiti as the revisit).
6. **Flat/egalitarian/privacy-max vs enterprise RBAC/governance** — admin-invisible private threads conflict with enterprise admin-visibility/audit/SSO expectations.

### Six strengths that generalize upward
Role/executor separation (re-tier, don't remove) · team-scoped RLS (extend to org→team→cross-team) ·
injection defense (more valuable at scale) · MCP-first/ADK/Cloud-Run-stateless already anticipates
connectors · `agent_runs` + compiled bi-temporal memory + diff cards = built-in observability ·
job-queue spine scales horizontally (ambient monitoring = new job types).

---

## Part 3 — Keep / Change / Add

**KEEP (the moat):** role-split as a structural security boundary (stronger than Lindy/Agentforce/
LangGraph, which gate at the app layer); compiled-not-authored memory + citations + bi-temporal +
reverts (where Zep/Graphiti/Mem0 independently landed); spotlighting + chat-as-data + compiler-only
writes (CaMeL-equivalent); server-bound tenant args; RLS + tenant_id + GUC (what Salesforce/Slack/AWS
use); the job-queue spine.

**CHANGE:** the `SET LOCAL` + pooling discipline; the superuser `user_session`; single synchronous
agent → real tool-loop; one-room → work-graph; consent boundary "visible" → "risk/reversibility".

**ADD:** production agent runtime (HTTP + event-driven + scheduled); per-team MCP connectors + token
vault; risk-tiered autonomy gate + Agent Inbox; ambient chat→memory loop; team↔team layer; optional
code-execution sandbox tier; real observability on `agent_runs`.

---

## Part 4 — Direct answers to the four questions

**Do similar solutions use a DB strategy like ours?** Yes — shared-Postgres + RLS + `tenant_id`-GUC
is exactly what Salesforce (8,000 orgs/instance), Slack, and AWS's reference architecture use; it's
the 2026 default through startup scale. What's distinctive and *better* about Comrade's is the
agent-vs-executor split as separate DB principals (underused industry-wide). What others do
differently at enterprise scale: a hybrid "peel-off" — heavy/compliance tenants moved to
schema-per-tenant or database-per-tenant (Neon's copy-on-write branching makes this cheap). Plan the
escape hatch; don't build it yet.

**How can an agent do tasks (VM)?** For ~95% of Comrade's work, no VM. Actions are structured
platform/connector operations → extend the `EXECUTOR` role into a connector-aware **action executor**
that injects the team's scoped token and calls the API. Safer than a sandbox because the action set
is finite and DB-enforced. A real microVM sandbox (rent E2B/Fly Firecracker — buy, don't build) is a
separate, later Tier 2, only for arbitrary code/computer-use, and still behind the consent gate.

**How will it connect to MCPs/plugins/skills/connectors?** Remote MCP (Streamable HTTP) + OAuth
2.1/PKCE per team; refresh tokens in a per-team encrypted vault keyed by the session's `team_id`
(never the LLM); read-only MCP tools → `AGENT` role, external-writes → propose→consent→execute;
deferred tool loading (ToolSearch-style — 50 tools ≈ 72K tokens); per-team allowlists; extend
spotlighting to every tool result (the lethal trifecta now lives in each connector). Skills = markdown
packs (frontmatter always loaded, body on demand); plugins = bundles. Platform tools stay native
functions; MCP is for external connectors.

**How to implement standard agentic capabilities?** Adopt Claude Code's loop: a streaming generator
where `tool_use` is the only continue signal; tools execute (in parallel) and feed back; compaction/
retry recovery around it; subagents/forks later. Make it event-driven (consume from an event bus:
@mention, scheduled tick, webhook, doc upload) rather than synchronous-only. Use `agent_runs` as the
step log for observability + resumability.

---

## Part 5 — Target architecture (mapped to Claude Code patterns)

```
                        ┌───────────────── EVENT BUS ─────────────────┐
   @mention · cron tick · GitHub webhook · doc upload · chat window full
                        └──────────────────────┬──────────────────────┘
                                               ▼
   ┌──────────────── PER-TEAM AGENT RUNTIME (the loop) ──────────────────┐
   │  stream → tool_use → execute → feed back → loop   [query.ts]         │
   │  records every step to agent_runs   [agent_runs = step log]          │
   └───────────────┬─────────────────────────────────────┬──────────────┘
                   ▼                                       ▼
   ┌─ TOOL / PERMISSION LAYER ───────────┐   ┌─ MEMORY & CONTEXT ──────────────┐
   │ platform tools (native fns)         │   │ compiled facts (pgvector) +     │
   │  + per-team MCP connectors (vaulted)│   │ agentic Grep/Read raw msgs      │
   │ merged → ONE gated list             │   │ ambient debounced compile loop  │
   │ deferred loading [ToolSearch]       │   │ compiler = quarantined LLM      │
   │ risk-tier classifier → auto|queue|block │ multi-scope (team vs member)    │
   └───────────────┬─────────────────────┘   └─────────────────────────────────┘
                   ▼
   ┌─ EXECUTION ─────────────────────────────────────────────────────────────────┐
   │ Tier 1: ACTION EXECUTOR (now) — EXECUTOR role + connector API calls           │
   │         [checkPermissions; propose→consent→execute = the gate]                │
   │ Tier 2: ephemeral Firecracker sandbox (later, rented) — only for code/CU      │
   │         [worktree/remote isolation]                                           │
   └──────────────────────────────────────────────────────────────────────────────┘

   DATA: shared Postgres + RLS + role-split (KEEP) · generic `conversations` object
         + Slack-Connect bridge table for team↔team (ADD)
```

**Autonomy layer (resolves the Leverage/Claude-tag tension):** keep propose→consent→execute, add a
Claude-auto-mode-style two-stage classifier before the consent queue (cheap yes/no on Flash, escalate
to Pro CoT only when flagged) that routes by `action_tier` to `auto | queue | block`. A per-team
`autonomy_policy` decides notify/ask/auto per tier, defaults conservative, ratchets up as the team
accrues clean executions (trust is *earned* — Anthropic data: ~20%→40%+ auto-approval over hundreds
of sessions). Even auto-approved actions still run through `EXECUTOR` (`status='auto_approved'`),
never the agent directly. Add an Agent Inbox (batched approvals, not chat spam) and a circuit breaker
(N consecutive rejects → drop a tier).

---

## Part 6 — Research findings by lens (condensed, with sources)

**Collaboration-platform architecture.** Universal pattern = one polymorphic object + parent pointer
(Notion block, Slack conversation, Linear issue). Keep RLS+GUC. Add a generic `conversations` object
decoupled from `team_id`. For team↔team, copy Slack Connect's single-canonical-copy + bridge-table
(not per-tenant replication); gate by membership join, not single-tenant RLS. Build a unified event
bus + trigger→condition→action rule engine; the AI member, cron, webhooks, and MCPs all become
producers/consumers on it. Treat the agent as a `conversation_member`. Avoid Linear-style local-first
sync (multi-year bet, wrong for server-authoritative consent). Sources: Notion data model; Slack
shared channels / real-time / Events API / Socket Mode; Jira automation; Linear webhooks; AWS RLS.

**Multi-tenant DB.** Keep shared RLS+tenant_id+GUC (Salesforce/Slack/AWS standard). Fix three things:
(1) txn-wrapped `set_config(..., true)` for pooler safety; (2) composite indexes with `team_id`
leading + `(SELECT current_setting(...))`-wrapped predicates (RLS ~100× slower without); (3) policies
in migrations + CI isolation tests (real failure = silent policy drift). Role-split is sound and
underused — keep as separate connection URLs, don't `SET ROLE` on a shared connection; audit
`SECURITY DEFINER`. Enterprise: peel heavy tenants to schema- or Neon-branch DB-per-tenant; track
Nile. Sources: AWS Prescriptive Guidance; Salesforce Architects; PlanetScale/Bytebase RLS footguns;
Neon db-per-tenant; Supabase Supavisor.

**Agent execution & sandboxing.** Two tiers; 95% in the cheap one. Tier 1 = action/tool executor
(you have it) — extend EXECUTOR to connector API calls with an egress proxy injecting credentials +
per-task short-lived tokens (Codex pattern: setup phase has network+secrets, agent phase offline).
Tier 2 = ephemeral Firecracker sandbox (E2B/Fly), buy not build, only for code/computer-use, behind
the same consent gate. Block 169.254.169.254 + RFC1918. Sources: OpenAI Codex security; Cursor
self-hosted agents; Modal/Devin; Cloudflare Sandboxes GA; Northflank sandboxing guides.

**MCP / connectors at scale.** Remote Streamable HTTP + OAuth 2.1/PKCE-S256 + RFC 9728/8707; act as
resource server; per-team encrypted token vault keyed by session team_id; read-only→AGENT,
external-write→consent; deferred tool loading (50 tools ≈ 72K tokens; defer lifted Opus 4.5 MCP eval
79.5%→88.1%, −85% tokens); per-team allowlists; CIMD over DCR; treat all tool results as untrusted
(lethal trifecta). Sources: MCP authorization spec; Claude/ChatGPT connectors; Anthropic advanced
tool use; Willison lethal trifecta.

**Team memory & ambient context.** Keep compiler-not-record + bi-temporal + citations + reverts
(ahead of most). Add: explicit INVALIDATE/DELETE + dedupe/entity-resolution (you have only
ADD/REVISE); a light entity graph; hybrid retrieval (compiled facts for "state" + agentic Grep/Read
raw messages for "what did X say"); ambient chat via **debounced micro-batch** compile jobs (N msgs /
T idle / high-signal event — not per-message, not weekly); a multi-scope visibility column (team vs
member); chat-derived facts carry lower confidence than document-derived. Compiler = your quarantined
LLM. Sources: Zep/Graphiti temporal KG; Mem0 State of Memory 2026; Graphlit survey; OWASP LLM01.

**Agent actions & autonomy.** Keep propose→consent→execute (stronger than app-layer Lindy/Agentforce/
LangGraph). Add: `action_tier` per tool (server-bound); a Claude-auto-mode two-stage classifier gate;
per-team `autonomy_policy` (tier→notify/ask/auto) defaulting conservative with an earned-trust
ratchet; auto-execute still via EXECUTOR; Agent Inbox; circuit breaker (3 consecutive / 20 total
denials). Sources: Anthropic Claude Code auto mode + measuring agent autonomy; LangChain ambient
agents + interrupts; Agentforce in Slack; MindStudio risk tiers.

---

## Part 7 — Phased roadmap

- **Phase 0 (finish the pilot core):** production entrypoint (HTTP service + event bus); real agent
  loop; start writing `agent_runs`; fix `SET LOCAL`/pooler discipline + superuser `user_session`;
  ambient chat→memory compile job (debounced). *Makes the designed thing actually run.*
- **Phase 1 (startup / multi-team):** per-team MCP connectors + token vault + deferred loading;
  `conversations` work-graph object; risk-tier autonomy gate + Agent Inbox; hybrid retrieval; compiler
  INVALIDATE/dedupe.
- **Phase 2 (enterprise):** team↔team via Slack-Connect canonical-copy + bridge; admin/RBAC/governance/
  audit/SSO; peel heavy tenants to schema-/DB-per-tenant; optional code-execution sandbox; EMA-style
  admin connector provisioning.

---

## Part 8 — Top risks & open decisions

- Injection surface grows with connectors + ambient memory — keep ambient chat→memory
  compiler-mediated; chat-derived facts lower confidence.
- Consent fatigue vs autonomy safety — solved by the risk-tier ladder; the tier boundaries are a
  product decision the owner owns.
- Privacy-maximalist design (admin-invisible private threads) conflicts with enterprise
  admin-visibility/governance — a conscious per-tier choice.
- Single-call compiler cost breaks at multi-team/multi-month scale (Graphiti is the named revisit).

---

## Part 9 — Next step

Chosen focus: **the production agent runtime** (Phase 0). The next deliverable is a design doc for
that runtime — the HTTP service, the event bus, the agent tool-loop, session/state backing, and
wiring `agent_runs` — before any code.
