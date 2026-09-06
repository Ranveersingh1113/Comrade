# Production Multiplayer Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn Comrade from one shared room plus private chats into a production-ready multiplayer cloud coding harness with first-class threads, durable runs, inline permissions, optional plans, a shared work board, isolated workspaces, attachments, scoped memory, extensible capabilities, and measurable agent quality.

**Architecture:** A thread is the unit of conversation, visibility, run serialization, working state, approvals, plans, files, and workspace ownership. Different threads run concurrently; one thread has one ordered run stream. Existing RLS roles and exact-action consent execution remain security boundaries, while detached approval UI and team-wide room locking are replaced. Work ships in independently deployable phases; every phase preserves old clients until its contract migration completes.

**Tech Stack:** Python 3.12, FastAPI, Google ADK 2.2, PostgreSQL/Supabase RLS and Realtime, React/TypeScript, pytest, Vitest, Playwright, Docker, Git/GitHub App.

**Spec:** `docs/ai-systems-atlas-priority-findings-2026-09-03.md`

## Global Constraints

- Preserve uncommitted/user-owned files. Start execution in an isolated worktree using `superpowers:using-git-worktrees`.
- Use TDD. Each behavior change starts with a test that fails for the intended reason.
- Use `apply_patch` for edits. Never overwrite whole user files to make a small change.
- No new runtime dependency unless stdlib, Postgres, Supabase, React, existing ADK, Docker, or Git cannot provide the behavior.
- RLS is the browser API. Every table gets RLS, explicit grants, cross-team denial tests, and immutable identity/provenance columns.
- Model never supplies `team_id`, `thread_id`, `requester_id`, `run_id`, workspace path, approval owner, or capability scope. Server binds them into session state.
- Untrusted chat, repository, attachment, memory, MCP, connector, command, and tool output remains datamarked before reaching the model.
- No model call or external network call inside a DB transaction.
- Keep `comrade_agent`, `comrade_executor`, `comrade_pipeline`, and `authenticated` privilege separation. Add a narrow control-plane role; never add `service_role`.
- Same thread: ordered execution. Different threads: concurrent execution. Same repository: isolated workspaces, never shared writable checkout.
- Plan tool is optional. Thread creation must not create a plan.
- Public work is visible on board. Restricted/private thread metadata and content stay invisible to non-participants.
- New threads default to team-visible. A member must deliberately choose restricted visibility; one selected participant is labelled Private, while two or more are labelled Selected members.
- Adding a participant to a restricted thread grants full thread history after an explicit warning to existing participants. Removing a participant revokes all future and historical access immediately. Record membership changes in audit history.
- Inline approval removes detached UX, not exact-action hashing, requester authorization, compare-and-swap execution, executor isolation, expiry, or audit history.
- Initial permission choices are Allow once, Allow for this thread, and Reject. Thread grants bind requester, tool, normalized resource constraint, thread, expiry, and risk ceiling. Destructive, secret-bearing, deployment, and repository-publication actions remain Allow once only.
- First cloud runtime supports finite commands and supervised development servers. Arbitrary persistent VMs, inbound raw TCP, and unrestricted egress are excluded.
- User-supplied executable plugins never load into FastAPI or worker processes. Skills are data; external executable extensions cross an MCP/connector isolation boundary.
- Run `scripts/gates.sh --with-reset` at every migration phase gate. Run live/model and real-GitHub lanes only where named.
- One implementer subagent at a time. After each task, dispatch a fresh reviewer against the task diff before continuing.

---

## Phase 0 — Trustworthy baseline and removal of obsolete behavior

### Task 1: Make the four-person scenario a fail-closed evaluation

**Files:**
- Modify: `sim/scenario.py`
- Create: `evaluation/team_scenario.py`
- Create: `tests/test_team_scenario_scoring.py`
- Modify: `pyproject.toml`
- Modify: `scripts/gates.sh`

**Interfaces:**
- Produces: `score_team_scenario(evidence: dict) -> dict` returning `passed`, `failures`, and `metrics`.
- Produces: `uv run python -m sim.scenario --check` with nonzero exit on infrastructure or required-outcome failure.

- [ ] Write scorer tests using static evidence for: missing HTTP result, missing repo clone, reply claiming three tasks when zero rows exist, forbidden memory request-facts, duplicate normalized facts, absent PR proposal, and absent verification before proposal.
- [ ] Run `uv run pytest tests/test_team_scenario_scoring.py -q`; confirm failures because scorer does not exist.
- [ ] Implement pure `score_team_scenario`. Treat DB rows and `agent_steps.response` as authority; never grade success from assistant prose alone.

```python
def score_team_scenario(evidence: dict) -> dict:
    failures: list[str] = []
    metrics = {
        "input_tokens": sum(r.get("input_tokens", 0) for r in evidence["runs"]),
        "output_tokens": sum(r.get("output_tokens", 0) for r in evidence["runs"]),
        "latency_seconds": sum(r.get("seconds", 0) for r in evidence["runs"]),
    }
    if evidence.get("http_errors"):
        failures.append("agent HTTP request failed")
    if not evidence.get("repo_cloned"):
        failures.append("repository did not clone")
    if len(evidence.get("task_consents", [])) != 3:
        failures.append("expected exactly three task actions")
    if len(evidence.get("pr_consents", [])) != 1:
        failures.append("expected exactly one PR action")
    return {"passed": not failures, "failures": failures, "metrics": metrics}
```

- [ ] Change `ask()` and clone wait to raise on failure under `--check`; cleanup failures must name table/error and fail check mode instead of disappearing.
- [ ] Collect runs, ordered steps, consent outcomes, active memory facts/citations, clone state, tokens, and latency into one evidence JSON artifact. Do not write JWTs into it.
- [ ] Add pytest marker `scenario` excluded from default gate; add `scripts/gates.sh --with-agent-eval` that runs deterministic scorer tests plus the live scenario when explicitly requested.
- [ ] Run deterministic tests, then one live scenario. Record baseline metrics without pass/fail token thresholds.
- [ ] Commit: `test: make team scenario fail closed`.

### Task 2: Remove `team_propose_batch` and preserve existing actions

**Files:**
- Modify: `agent/agent.py`
- Modify: `agent/tools.py`
- Modify: `agent/registry.py`
- Modify: `evaluation/scenarios.py`
- Modify: `tests/test_agent.py`
- Modify: `tests/test_tool_registry.py`
- Modify: `tests/test_departure_request.py`
- Modify: `tests/test_propose_batch.py`

**Interfaces:**
- Removes model tool: `team_propose_batch`.
- Preserves executor actions: `task_create`, `task_update`, `repo_open_pr`.
- Preserves existing `consent_queue` rows and `batch_id` until detached inbox removal.

- [ ] Add a registry test asserting `team_propose_batch` is unknown and absent from root-agent tools.
- [ ] Run focused tests and confirm failure while tool remains registered.
- [ ] Delete `team_propose_batch`, `_BATCH_ALIASES`, imports, prompt guidance, ToolSpec, scenario expectations, and tests whose only purpose is model-side batching.
- [ ] Keep `shared.consent.propose_batch` temporarily only if existing UI/tests require historical batch rendering. Mark removal in Task 12, not with a speculative compatibility abstraction.
- [ ] Run `uv run pytest tests/test_agent.py tests/test_tool_registry.py tests/test_departure_request.py tests/test_propose_batch.py -q`.
- [ ] Commit: `refactor: remove batch proposal tool`.

### Task 3: Bind consent resolution to the correct team

**Files:**
- Modify: `shared/consent.py`
- Modify: `server/app.py`
- Test: `tests/test_consent.py`
- Test: `tests/test_consent_tiers.py`

**Interfaces:**
- `approve_consent`, `reject_consent`, and `edit_and_approve` require both consent ID and matching team under requester RLS.

- [ ] Add tests where requester owns consent in team A but submits team B. Assert row remains `pending`, no executor runs, and API returns not-found/not-yours.
- [ ] Confirm tests fail because current requester update filters by ID only.
- [ ] Add `team_id = %s` to every requester-side select/update and retain executor-side team session.
- [ ] Run focused consent tests.
- [ ] Commit: `fix: bind consent resolution to team`.

### Task 4: Require verification after the latest repository edit

**Files:**
- Modify: `agent/repo_tools.py`
- Modify: `agent/runtime.py`
- Test: `tests/test_repo_tools_write.py`
- Test: `tests/test_repo_run.py`

**Interfaces:**
- Session state keys: `repo_edit_generation: int`, `repo_verified_generation: int | None`.
- `repo_edit` increments edit generation after successful write.
- Successful, non-timeout `repo_run` with exit code `0` records current generation as verified.
- `repo_propose_pr` refuses when current generation is unverified.

- [ ] Add tests: edit then propose refuses; edit then failed run refuses; edit then passing run proposes; second edit invalidates earlier verification; no-change PR still refuses through existing empty-diff behavior.
- [ ] Confirm tests fail against current permissive proposal.
- [ ] Initialize state server-side in runtime. Update state only after successful tool outcomes; model cannot set either key.
- [ ] Return explicit refusal: `Run a relevant test, build, lint, or executable check after your latest edit before proposing this pull request.`
- [ ] Run repository tool tests.
- [ ] Commit: `feat: require verification before PR proposal`.

---

## Phase 1 — First-class threads and visibility

### Task 5: Expand database with canonical threads

**Files:**
- Create: `supabase/migrations/20260904090000_threads_expand.sql`
- Create: `tests/test_threads.py`
- Modify: `tests/_seed.py`
- Modify: `tests/test_wiring.py`

**Interfaces:**
- `threads(id, team_id, title, visibility, kind, work_state, owner_id, due_at, created_by, created_at, updated_at, archived_at)`.
- `visibility`: `team | restricted`. Restricted with one participant is private; multiple participants is selected-members.
- `kind`: `discussion | work`.
- `work_state`: null for discussion; `planned | active | waiting | review | done` for work.
- `thread_participants(thread_id, team_id, user_id, added_by, joined_at)`.
- Adds nullable `messages.thread_id`, `agent_runs.thread_id`, `agent_runs.requester_id`, and `agent_runs.input_message_id`.

- [ ] Write SQL/Python tests for public visibility, restricted participant visibility, outsider denial, hidden restricted metadata, cross-team FK rejection, immutable team/thread/sender identity, and legacy row backfill.
- [ ] Create tables with composite `(id, team_id)` uniqueness where child rows carry both IDs. Enable RLS before grants.
- [ ] Add `can_access_thread(thread_id, user_id)` as `security definer`, fixed `search_path`, stable SQL function. Team threads require active team membership; restricted threads additionally require participant membership.
- [ ] Revoke ambient Supabase grants. Grant authenticated users only the operations required by policies. Add matching grants for worker roles in same migration.
- [ ] Backfill one `General` team thread per team and one restricted legacy private thread per `(team_id, thread_owner_id)` found in messages.
- [ ] Add temporary compatibility trigger mapping old `thread_type/thread_owner_id` inserts to canonical thread IDs. Reject ambiguous restricted inserts.
- [ ] Add indexes `(thread_id, created_at, id)`, `(team_id, updated_at desc)`, and `(user_id, thread_id)`.
- [ ] Run `npx supabase db reset`, restore local roles, then thread/wiring/RLS suites.
- [ ] Commit: `feat: add canonical thread model`.

### Task 6: Route server, history, runs, and locks by thread ID

**Files:**
- Modify: `server/app.py`
- Modify: `agent/history.py`
- Modify: `agent/runtime.py`
- Modify: `shared/agent_runs.py`
- Modify: `shared/db.py`
- Modify: `shared/nudge.py`
- Modify: `pipeline/compiler.py`
- Test: `tests/test_server.py`
- Test: `tests/test_server_stream.py`
- Test: `tests/test_agent_history.py`
- Test: `tests/test_agent_runs.py`
- Test: `tests/test_room_lock.py`

**Interfaces:**
- Turn requests accept `thread_id`; legacy `thread_type` remains during compatibility phase only.
- `recent_turns(team_id, requester_id, thread_id, limit, exclude_message_id)`.
- `start_run(team_id, requester_id, thread_id, input_message_id, trigger_type, input_summary)`.
- `thread_lock(thread_id)` replaces `room_lock(team_id)`.

- [ ] Add tests proving same-thread lock refusal, different-thread concurrency within one team, private/restricted thread locking, inaccessible thread rejection before message persistence, history isolation between two visible team threads, and run provenance.
- [ ] Add participant-history tests: an invited participant receives full history only after the explicit invite flow; a removed participant immediately loses message, attachment, plan, consent, workspace, and board-card access.
- [ ] Resolve and authorize thread server-side as requesting user. Bind canonical IDs into ToolContext state.
- [ ] Replace history mode/owner filtering with exact thread ID.
- [ ] Attach nudge messages to recipient's canonical restricted thread and compiler notices to General thread.
- [ ] Keep one repository-level write lock around legacy shared checkout mutations until Task 11 introduces isolated workspaces.
- [ ] Run focused backend suite.
- [ ] Commit: `feat: scope agent turns to threads`.

### Task 7: Ship thread UI and explicit Team/Comrade composer mode

**Files:**
- Modify: `frontend/src/lib/types.ts`
- Modify: `frontend/src/lib/agentApi.ts`
- Modify: `frontend/src/hooks/useMessages.ts`
- Modify: `frontend/src/state/TeamContext.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/components/Sidebar.tsx`
- Create: `frontend/src/screens/Threads.tsx`
- Create: `frontend/src/components/ComposerMode.tsx`
- Modify: `frontend/src/screens/GroupRoom.tsx`
- Modify: `frontend/src/screens/PrivateThread.tsx`
- Test: `frontend/tests/component/Threads.test.tsx`
- Test: `frontend/tests/component/ComposerMode.test.tsx`
- Test: `frontend/tests/integration/rls-messages.test.ts`
- Test: `frontend/tests/e2e/journeys.spec.ts`

**Interfaces:**
- Routes: `/threads` and `/threads/:threadId`; `/room` redirects to General thread; `/thread` redirects to current user's legacy private thread.
- Composer modes: `team | agent`, stored per `(user_id, thread_id)` in browser storage.
- Team mode inserts human message. Agent mode invokes `/agent/turn/stream`. Explicit `@comrade` invokes agent from Team mode.

- [ ] Write component and integration tests for creating team/restricted/private threads, selecting participants, hidden restricted metadata, mode persistence per user/thread, visible mode label, keyboard focus, and exact `thread_id` payload.
- [ ] Build thread list and route using direct Supabase reads under RLS.
- [ ] Reuse current room/private message rendering; do not fork three chat implementations.
- [ ] Default General/discussion composer to Team. Default a new work thread composer to Agent. Keep mode visibly labelled beside send control.
- [ ] Add Realtime publication and `REPLICA IDENTITY FULL` only for tables subscribed here.
- [ ] Run Vitest, integration tests, typecheck, lint, and Playwright thread journey.
- [ ] Commit: `feat: add collaborative thread interface`.

### Task 8: Contract migration away from legacy message modes

**Files:**
- Create: `supabase/migrations/20260904100000_threads_contract.sql`
- Modify: `pipeline/chat.py`
- Modify: `server/app.py`
- Modify: `agent/tools.py`
- Modify: `frontend/src/lib/types.ts`
- Test: `tests/test_chat_memory.py`
- Test: `tests/test_read_tools.py`
- Test: `tests/test_team_export.py`

**Interfaces:**
- All new messages require `thread_id`.
- Public-memory compilation selects eligible team-visible threads, not every restricted thread.

- [ ] Add tests proving restricted/private messages never compile into team wiki, message search respects accessible thread IDs, and exports contain only requester-visible thread metadata/content.
- [ ] Switch chat sweeps, remember checks, search, contribution recency, and export to canonical thread joins.
- [ ] Make `messages.thread_id` non-null after orphan audit. Remove compatibility trigger and application writes of legacy identity.
- [ ] Keep legacy columns for one release only if a read path still consumes them; otherwise drop constraints, indexes, and columns in this migration.
- [ ] Run reset gate and frontend integration gate.
- [ ] Commit: `refactor: complete thread identity migration`.

---

## Phase 2 — Durable run queue, sessions, steering, and stopping

### Task 9: Turn agent runs into a leased per-thread queue

**Files:**
- Create: `supabase/migrations/20260904110000_agent_run_queue.sql`
- Create: `agent/run_queue.py`
- Create: `agent/worker.py`
- Modify: `server/app.py`
- Modify: `shared/agent_runs.py`
- Modify: `agent/runtime.py`
- Test: `tests/test_agent_run_queue.py`
- Test: `tests/test_agent_worker.py`

**Interfaces:**
- Statuses: `queued | running | waiting_for_permission | waiting_for_user | done | failed | cancelled`.
- `enqueue_turn(...) -> run_id` persists message/run atomically after authorization and budget reservation.
- `claim_next_run(worker_id) -> Run | None` uses `FOR UPDATE SKIP LOCKED`, requires no other active run for same thread, and sets lease expiry.
- `renew_lease`, `cancel_run`, and expired-lease recovery.

- [ ] Add concurrency tests: two runs same thread execute in order; two different threads claim concurrently; expired run is reclaimed; final-attempt expiry fails; cancellation prevents further tools; worker crash leaves durable input.
- [ ] Add unique partial index allowing one `running|waiting_*` run per thread.
- [ ] Move long agent execution out of HTTP request lifecycle. POST returns run ID; streaming endpoint reads durable run events and terminates on terminal/waiting state.
- [ ] Reuse `agent_steps` as durable event log. Add event kinds only where current columns cannot represent user steering, permission result, or lifecycle transitions.
- [ ] Start worker with `uv run python -m agent.worker`; add fresh-interpreter handler/wiring test matching pipeline worker regression pattern.
- [ ] Run concurrency and crash-recovery tests.
- [ ] Commit: `feat: add durable per-thread agent queue`.

### Task 10: Add steering and restart-safe continuation

**Files:**
- Modify: `agent/run_queue.py`
- Modify: `agent/runtime.py`
- Modify: `agent/history.py`
- Modify: `server/app.py`
- Modify: `frontend/src/lib/agentApi.ts`
- Modify: `frontend/src/screens/Threads.tsx`
- Test: `tests/test_agent_steering.py`
- Test: `tests/test_agent_resume.py`
- Test: `frontend/tests/component/Threads.test.tsx`

**Interfaces:**
- New participant messages during a running tool call append as steering events.
- Runtime injects queued steering before next model call, not during an irreversible tool execution.
- Restart reconstructs context from thread messages, pinned state, plan, run steps, and last tool outcome.

- [ ] Add tests for steer-before-next-model-call, cancellation while tool runs, worker restart after completed tool, no duplicate tool execution, and participant/non-participant authorization.
- [ ] Add idempotency key to effectful tool steps and record result before next model call.
- [ ] Rehydrate into a fresh ADK in-memory session from Comrade-owned tables; do not create unqualified ADK session tables in `public`.
- [ ] Use a compact continuation record containing constraints, current plan version, workspace revision, completed effects, pending approval, and unresolved question. Preserve structured fields verbatim.
- [ ] Run resume tests with simulated worker termination.
- [ ] Commit: `feat: steer and resume thread runs`.

---

## Phase 3 — Inline permissions; detached consent UI removed

### Task 11: Attach approvals to threads and pause runs

**Files:**
- Create: `supabase/migrations/20260904120000_consent_thread_provenance.sql`
- Create: `supabase/migrations/20260904120100_permission_grants.sql`
- Modify: `shared/consent.py`
- Modify: `agent/permission_plugin.py`
- Modify: `agent/runtime.py`
- Modify: `server/app.py`
- Test: `tests/test_consent.py`
- Test: `tests/test_consent_column_guard.py`
- Test: `tests/test_agent_resume.py`

**Interfaces:**
- Consent rows require `thread_id` and `agent_run_id` for agent-originated actions.
- Thread participants may read actions in accessible threads; only immutable `requesting_member_id` may approve/edit/reject.
- Pending permission transitions run to `waiting_for_permission`; resolution requeues same run.
- `permission_grants` binds `requesting_member_id`, `thread_id`, tool name, normalized resource constraint, expiry, and maximum risk class. High-risk actions cannot create reusable grants.

- [ ] Add tests for provenance, participant read, non-participant denial, requester-only resolution, immutable identity, exact hash after edit, duplicate approval CAS, rejection reason reaching continuation, expiry, and resume without repeating prior effects.
- [ ] Add tests for Allow once, thread grant reuse, resource mismatch, expiry, revocation, requester mismatch, and refusal to persist a grant for destructive/deployment/secret/publication actions.
- [ ] Bind thread/run provenance inside ToolContext. Never accept either as model tool arguments.
- [ ] Keep existing executor registry and DB role. Approval runs executor first, records outcome, then requeues continuation.
- [ ] For public/shared threads, show exact requested action to participants but approval controls only to requester. Restricted thread RLS protects details.
- [ ] Route non-agent departure requests into target member's restricted system thread.
- [ ] Run consent, RLS, and resume suites.
- [ ] Commit: `feat: pause runs for inline permission`.

### Task 12: Render approval cards inline and delete inbox workflow

**Files:**
- Modify: `frontend/src/components/ConsentCard.tsx`
- Modify: `frontend/src/screens/Threads.tsx`
- Modify: `frontend/src/hooks/useRealtime.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/components/Sidebar.tsx`
- Delete: `frontend/src/screens/ConsentInbox.tsx`
- Modify: `shared/consent.py`
- Modify: `tests/test_propose_batch.py`
- Test: `frontend/tests/component/Threads.test.tsx`
- Test: `frontend/tests/component/ConsentCard.test.tsx`
- Test: `frontend/tests/e2e/journeys.spec.ts`

**Interfaces:**
- Thread timeline merges messages and requester-visible consent events by server timestamp.
- Existing card approve/edit/reject controls remain; resolved result appears at same timeline position.

- [ ] Add UI tests for pending, approved/executed, rejected-with-reason, expired, and resumed-agent states inside thread.
- [ ] Subscribe to consent changes scoped through RLS and thread filter.
- [ ] Remove inbox route/nav only after all non-expired legacy rows are migrated to a visible system thread. Add migration/report command that refuses removal while orphan pending rows exist.
- [ ] Remove `propose_batch`, batch-only UI grouping, batch-only tests, and `batch_id` column after orphan audit proves no pending batch depends on it.
- [ ] Run frontend component, integration, and Playwright approval journey.
- [ ] Commit: `refactor: move approvals into threads`.

---

## Phase 4 — Optional plan tool and shared work board

### Task 13: Add optional thread plan state

**Files:**
- Create: `supabase/migrations/20260904130000_thread_plans.sql`
- Create: `agent/plan_tools.py`
- Modify: `agent/agent.py`
- Modify: `agent/registry.py`
- Modify: `agent/runtime.py`
- Test: `tests/test_plan_tools.py`
- Test: `tests/test_tool_registry.py`

**Interfaces:**
- `thread_plans(thread_id primary key, team_id, version, steps jsonb, updated_by_kind, updated_at)`.
- Step shape: `{id: str, text: str, status: "pending"|"active"|"completed"|"blocked"}`.
- Tool: `plan_update(steps: list[PlanStep], expected_version: int | None)`; thread/team/run identity bound in state.

- [ ] Add tests for no plan on thread creation, create/update/CAS conflict, participant visibility, cross-thread denial, one-active-step validation, and no human approval requirement.
- [ ] Register plan tool as thread-scoped write with `needs_human=False` because it has no external side effect.
- [ ] Prompt guidance: use plan only for dependent multi-step, long-running, collaborative, or explicitly requested work; skip questions, searches, quick checks, and obvious small edits.
- [ ] Include plan version and incomplete steps in continuation state and compaction pins.
- [ ] Run plan and registry tests.
- [ ] Commit: `feat: add optional thread plans`.

### Task 14: Replace Tasks screen with thread-derived work board

**Files:**
- Modify: `frontend/src/screens/Tasks.tsx` or rename to `frontend/src/screens/WorkBoard.tsx`
- Modify: `frontend/src/hooks/useTasks.ts` or replace with `frontend/src/hooks/useWorkThreads.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/components/Sidebar.tsx`
- Modify: `frontend/src/screens/Threads.tsx`
- Create: `supabase/migrations/20260904140000_tasks_to_work_threads.sql`
- Test: `frontend/tests/component/WorkBoard.test.tsx`
- Test: `frontend/tests/integration/rls-threads.test.ts`
- Test: `frontend/tests/e2e/journeys.spec.ts`

**Interfaces:**
- Board columns: `planned`, `active`, `waiting`, `review`, `done`.
- Card derives thread title, owner, accessible participants, due date, plan progress, last activity, and current agent state.

- [ ] Add tests proving discussion threads stay off board, work thread may exist without plan, plan may exist without closing thread, public cards are team-visible, restricted/private cards are absent to non-participants, and drag/state update obeys RLS.
- [ ] Migrate existing task rows into team-visible work threads with owner and due date; preserve original task ID in migration metadata for audit/export.
- [ ] Remove AI task proposal/update tools after migrated board flow has no callers. Human board edits go directly through RLS; agent board changes occur only from explicit request or lifecycle rule.
- [ ] Auto-mark work `active` when first meaningful agent execution/edit begins. Never auto-close from plan completion.
- [ ] Run frontend and migration gates.
- [ ] Commit: `feat: derive shared board from work threads`.

---

## Phase 5 — Isolated cloud workspaces and supervised servers

### Task 15: Give each work thread an isolated Git workspace

**Files:**
- Modify: `shared/workspace.py`
- Modify: `pipeline/repo_sync.py`
- Modify: `agent/repo_tools.py`
- Modify: `agent/runtime.py`
- Create: `tests/test_thread_workspaces.py`
- Modify: `tests/test_repo_tools_write.py`

**Interfaces:**
- `workspace_for(team_id, thread_id, repo_full_name) -> Path`.
- Workspace records base commit, current commit, dirty state, last use, and cleanup state.
- One thread/repo workspace; different threads never share writable files.

- [ ] Add tests for two threads editing same repo without collision, restart persistence, base-ref refresh conflict, cleanup refusing active workspace, hidden `.git` from sandbox, and path traversal/cross-team denial.
- [ ] Use Git worktrees from a credential-free mirror. Credential minting remains in sync/push operations only.
- [ ] Route every repo tool through server-bound thread workspace path.
- [ ] Replace temporary repo write lock with workspace isolation; keep per-workspace mutation lock.
- [ ] Run git seam tests and `pytest -m realgithub` before phase completion.
- [ ] Commit: `feat: isolate repository work by thread`.

### Task 16: Harden command sandbox and add process supervision

**Files:**
- Modify: `docker/sandbox.Dockerfile`
- Modify: `agent/sandbox.py`
- Create: `agent/processes.py`
- Create: `supabase/migrations/20260904150000_sandbox_processes.sql`
- Modify: `agent/repo_tools.py`
- Test: `tests/test_repo_run.py`
- Create: `tests/test_sandbox_processes.py`

**Interfaces:**
- Finite commands retain `repo_run`.
- Background tools: `process_start(command, port | None)`, `process_logs(process_id)`, `process_stop(process_id)`.
- Process states: `starting | running | exited | failed | stopped | expired`.

- [ ] Add tests for non-root UID, read-only dependency mount, no platform secrets, CPU/memory/PID/disk/time limits, timeout kill, workspace confinement, process ownership by thread, log clipping, idle expiry, and cleanup after worker crash.
- [ ] Run containers as fixed non-root UID/GID compatible with workspace ownership. Use disposable writable workspace layer; do not let test caches create host root-owned files.
- [ ] Keep default network `none`. Dependency setup uses explicit registry egress proxy; replace `PLANNED_SETUP_EGRESS_ALLOWLIST` with enforced policy and invert current test that proves open internet.
- [ ] Development-server containers join an internal preview network that accepts traffic only from the authenticated preview proxy and has no default external route. Never switch preview processes to unrestricted bridge networking.
- [ ] Supervise detached containers by persisted process ID/container ID. Reconciler kills orphaned/expired containers and reports failure; no silent cleanup.
- [ ] Run Docker sandbox suite on Linux-compatible CI, not Docker Desktop alone.
- [ ] Commit: `feat: supervise isolated sandbox processes`.

### Task 17: Add authenticated preview URLs

**Files:**
- Create: `server/previews.py`
- Modify: `server/app.py`
- Modify: `agent/processes.py`
- Modify: `frontend/src/screens/Threads.tsx`
- Create: `tests/test_previews.py`
- Test: `frontend/tests/component/Threads.test.tsx`

**Interfaces:**
- Only explicitly declared process port can be previewed.
- Preview token binds user, team, thread, process, port, and short expiry.
- Proxy verifies current thread access on every new connection.

- [ ] Add tests for participant access, non-participant denial, expiry, stopped-process denial, host-header defense, arbitrary-port denial, path/websocket proxying, request/body limits, and no token in application logs.
- [ ] Expose authenticated HTTPS route through reverse proxy; never publish Docker host ports directly.
- [ ] Render preview link/status in thread timeline and work card.
- [ ] Run security and browser preview journey.
- [ ] Commit: `feat: add authenticated thread previews`.

### Task 18: Route GitHub CI feedback into originating work thread

**Files:**
- Modify: `server/app.py`
- Modify: `pipeline/github.py`
- Modify: `shared/github_app.py`
- Modify: `agent/run_queue.py`
- Modify: `frontend/src/screens/Threads.tsx`
- Create: `tests/test_github_ci_feedback.py`
- Modify: `tests/test_github_webhook.py`

**Interfaces:**
- Handle GitHub `check_suite`, `check_run`, and workflow completion deliveries idempotently.
- Correlate repository, branch/PR, consent provenance, workspace, and thread.
- Failed CI appends a thread event with check name, conclusion, URL, and clipped/datamarked failure summary; active work may enqueue a continuation under existing budget and permission policy.

- [ ] Add tests for signed delivery, duplicate delivery, unknown repository/branch, cross-team isolation, success/failure/cancelled conclusions, untrusted log marking, clipped output, and one continuation per terminal failure revision.
- [ ] Persist GitHub delivery ID and correlation before acknowledging webhook. Return success only after durable enqueue/event write.
- [ ] Fetch logs through GitHub App credentials server-side. Never place installation tokens in agent context or sandbox.
- [ ] Show CI event and link inside originating thread. Use webhook events; never poll-and-sleep inside agent run.
- [ ] Run webhook, GitHub ingestion, thread, and real-GitHub lanes.
- [ ] Commit: `feat: return CI results to work threads`.

---

## Phase 6 — Thread attachments and memory architecture

### Task 19: Extend existing document upload into thread attachments

**Files:**
- Create: `supabase/migrations/20260904160000_thread_attachments.sql`
- Modify: `frontend/src/screens/Documents.tsx`
- Modify: `frontend/src/screens/Threads.tsx`
- Modify: `frontend/src/lib/agentApi.ts`
- Modify: `server/app.py`
- Modify: `pipeline/compiler.py`
- Create: `tests/test_thread_attachments.py`
- Test: `frontend/tests/e2e/journeys.spec.ts`

**Interfaces:**
- `message_attachments(message_id, thread_id, document_id, purpose)`.
- Purpose: `turn_context | thread_artifact | team_knowledge`.
- Upload defaults to `turn_context`; promotion to team knowledge requires explicit member action.

- [ ] Add storage/RLS tests for participant-only access, cross-thread denial, size/type rejection, deleted attachment, private attachment excluded from wiki, explicit team promotion, and citation back to original file.
- [ ] Reuse private Supabase documents bucket and document parser. Remove current double upload by worker fetching authorized `storage_path`.
- [ ] Datamark parsed content at model boundary, preserve original binary, clip observations, and expose full content through explicit read tool.
- [ ] Add composer attachment control with upload/progress/failure states.
- [ ] Run upload, RLS, parser, and E2E tests.
- [ ] Commit: `feat: add scoped thread attachments`.

### Task 20: Separate working, personal, team, episodic, and procedural memory

**Files:**
- Create: `supabase/migrations/20260904170000_memory_scopes.sql`
- Modify: `pipeline/chat.py`
- Modify: `pipeline/compiler.py`
- Modify: `pipeline/wiki.py`
- Modify: `agent/agent.py`
- Modify: `agent/tools.py`
- Modify: `agent/history.py`
- Create: `tests/test_memory_scopes.py`
- Modify: `tests/test_chat_memory.py`
- Modify: `tests/test_extraction_recall_live.py`

**Interfaces:**
- Team wiki remains cited/versioned semantic memory.
- Thread working memory stores pinned constraints, rolling summary, open questions, plan pointer, workspace revision, and pending permission.
- Personal notebook is requester-private and opt-in.
- Agent steps/runs are episodic audit memory, retrieved only through scoped summarization.
- Skills are procedural memory handled by Task 21.
- Fact trust: `observed | proposed | confirmed | verified | superseded`.

- [ ] Add tests for scope isolation, public decision promotion, request-to-agent exclusion, duplicate consolidation, supersession, provenance, personal opt-in, compaction survival of pinned constraints, and malicious-memory datamarking.
- [ ] Change compiler rubric: direct requests to Comrade are not team decisions; questions are not facts; explicit decisions/assignments/constraints may promote; private/restricted content never promotes without explicit action.
- [ ] Keep lightweight wiki index but cap it by budget. Retrieve candidate pages broadly, rerank, then load only relevant facts/citations. Measure index, history, memory, tool schema, and observation tokens separately.
- [ ] Use sliding recent history plus rolling summary and pinned state. Never summarize identity, permission owner, exact approved args, file paths, current diff, or unresolved constraints.
- [ ] Run deterministic memory suite and repeated live extraction/retrieval eval before accepting behavior change.
- [ ] Commit: `feat: add scoped memory layers`.

---

## Phase 7 — Skills, MCP, connectors, and plugin boundary

### Task 21: Add one capability registry and slash-command skills

**Files:**
- Create: `supabase/migrations/20260904180000_capabilities.sql`
- Create: `agent/capabilities.py`
- Create: `agent/skills.py`
- Modify: `agent/agent.py`
- Modify: `agent/registry.py`
- Create: `frontend/src/components/SlashMenu.tsx`
- Modify: `frontend/src/screens/Threads.tsx`
- Create: `tests/test_capabilities.py`
- Create: `tests/test_skills.py`
- Test: `frontend/tests/component/SlashMenu.test.tsx`

**Interfaces:**
- Capability metadata: ID, kind, name, origin, version/digest, owner scope, enabled scope, declared permissions, secret references, network policy, risk class, install actor/time, revoked time.
- Skill source: repository `.comrade/skills/<name>/SKILL.md` or team-uploaded text package. Skill content is data, never imported Python.
- `/skill-name arguments` explicitly selects one skill; menu lists only capabilities readable/enabled for current thread/user.

- [ ] Add tests for digest/version changes, user/team/thread enablement, cross-team denial, revocation during run, malicious skill text, slash parsing/autocomplete, name collision, and capability audit events.
- [ ] Load skill metadata index into prompt; read full body only when invoked/selected.
- [ ] Run all skill text through same datamarking/instruction-precedence boundary as repository guides.
- [ ] Do not add arbitrary plugin runtime.
- [ ] Run capability, prompt-injection, frontend, and RLS tests.
- [ ] Commit: `feat: add scoped skills and slash commands`.

### Task 22: Add isolated MCP and OAuth connector adapters

**Files:**
- Create: `agent/mcp_client.py`
- Create: `server/connectors.py`
- Modify: `agent/capabilities.py`
- Modify: `agent/permission_plugin.py`
- Modify: `shared/config.py`
- Create: `tests/test_mcp_client.py`
- Create: `tests/test_connectors.py`

**Interfaces:**
- Remote MCP over approved HTTPS endpoints only; schema snapshot stored with digest.
- OAuth secrets stored as opaque references in server-side secret manager; model and sandbox never receive refresh tokens.
- Every external tool call passes capability scope, permission policy, timeout, output limit, audit, circuit breaker, and datamarking.

- [ ] Add tests for SSRF/private-IP rejection, DNS rebinding defense, TLS requirement, timeout, oversized/schema-poisoned result, revoked capability, secret non-disclosure, per-tenant circuit breaker, rate limit, and inline approval for side effects.
- [ ] Map connectors into same capability registry. Read-only tools may auto-run under policy; writes use inline permission unless explicit stored grant allows exact scope.
- [ ] Treat installable executable plugins as externally hosted MCP/connector services. Never import them into Comrade process.
- [ ] Run adversarial security suite.
- [ ] Commit: `feat: add isolated external capabilities`.

---

## Phase 8 — Quotas, least privilege, continuous evaluation, and operations

### Task 23: Add atomic usage reservation and narrow control-plane role

**Files:**
- Create: `supabase/migrations/20260904190000_usage_reservations.sql`
- Create: `supabase/migrations/20260904200000_control_plane_grants.sql`
- Modify: `scripts/restore_local_roles.py`
- Modify: `shared/db.py`
- Modify: `server/app.py`
- Modify: `shared/agent_runs.py`
- Modify: `pipeline/worker.py`
- Modify: `pipeline/repo_env.py`
- Modify: `pipeline/repo_sync.py`
- Create: `tests/test_usage_reservations.py`
- Modify: `tests/test_wiring.py`

**Interfaces:**
- `reserve_turn(team_id, run_id, token_reservation) -> Reservation` is atomic.
- `finalize_reservation(run_id, actual_tokens)` reconciles exactly once.
- `Role.CONTROL` may claim/finish queue rows and enumerate reconciliation candidates, but cannot read messages, memory bodies, documents, consent args, repository content, or private run steps.

- [ ] Add concurrent tests proving turn cap cannot overshoot, token reservations cannot overshoot, failure releases unused reservation, crash expiry reconciles, duplicate finalization is idempotent, and different teams do not block each other.
- [ ] Reserve before persisting user message or beginning stream. Use one DB function/transaction over a per-team hourly bucket; do not rely on aggregate precheck.
- [ ] Reconcile actual usage in `finally`, including failed/cancelled/empty turns.
- [ ] Create login/group role through restore/deployment script and grants through migration, matching project role convention.
- [ ] Replace production `Role.ADMIN` queue claims/sweeps. Keep ADMIN test-only.
- [ ] Run concurrency, wiring, reset, and full backend suites.
- [ ] Commit: `feat: enforce atomic quotas with least privilege`.

### Task 24: Expand harness evaluation into release gate

**Files:**
- Modify: `evaluation/scenarios.py`
- Modify: `evaluation/scoring.py`
- Modify: `evaluation/runner.py`
- Modify: `evaluation/run.py`
- Create: `evaluation/adversarial.py`
- Modify: `scripts/gates.sh`
- Create: `tests/test_eval_scoring.py`
- Create: `tests/test_adversarial_eval.py`

**Interfaces:**
- Every case records outcome, trajectory, security violations, memory precision/recall, tokens, cost, latency, model/version, prompt digest, capability digest, and environment digest.
- Repeated runs report pass interval and distribution; no single stochastic run decides release.

- [ ] Add cases for thread isolation, simultaneous threads, same-thread queueing, steering, cancel/resume, inline approval, rejection feedback, optional plan selection, board visibility, workspace isolation, server preview, attachment poisoning, memory promotion, MCP poisoning, and direct/indirect injection.
- [ ] Prefer deterministic outcome checks. Use human-calibrated judge only where no deterministic verifier exists; store rubric version and blind calibration set.
- [ ] Run at least five samples per stochastic case before setting floors. Gate regressions against measured confidence interval plus absolute security invariants.
- [ ] Report quality, tokens/cost, and latency together. Add context attribution by system prompt, tool schemas, history, memory, retrieved files, and observations.
- [ ] Keep held-out cases outside normal development output. Add sanitized production failures to regression set.
- [ ] Add scheduled full eval and small merge-gating smoke set. CI feedback arrives by event/webhook, never polling sleep loops.
- [ ] Run full eval twice from fresh pinned environments and compare reproducibility.
- [ ] Commit: `test: gate multiplayer harness quality`.

### Task 25: Add production operations and recovery gates

**Files:**
- Modify: `server/app.py`
- Modify: `pipeline/worker.py`
- Modify: `agent/worker.py`
- Modify: `shared/db.py`
- Modify: `shared/config.py`
- Create: `docs/operations.md`
- Create: `tests/test_readiness.py`
- Create: `tests/test_graceful_shutdown.py`

**Interfaces:**
- `/health` reports process liveness without dependency restart loops.
- `/ready` checks database, required migrations, worker lease freshness, sandbox backend, and configured secret providers.
- Every request/run/job log carries correlation ID, team ID hash, thread ID, run ID, and job ID where applicable; never raw tokens, secrets, private prompts, or attachment bodies.

- [ ] Add tests for liveness during DB outage, readiness failure for DB/migration/worker/sandbox faults, graceful drain without new claims, lease recovery, secret redaction, and clean pool/container shutdown.
- [ ] Add structured logs and counters for queue depth, lease expiry, tool failures, approval wait, sandbox starts/timeouts, memory retrieval, token/cost, and cross-boundary denials. Use existing logging/metrics platform adapter; core code exposes plain counters/events.
- [ ] Document deploy order, expand/contract migrations, rollback limits, backup/restore drill, key rotation, GitHub webhook rotation, incident disable switches, retention windows, and worker capacity assumptions with measured values from load tests.
- [ ] Run restore drill against disposable local database and verify team/thread/message/memory/consent/workspace metadata recovery.
- [ ] Run full reset gates, scenario eval, adversarial eval, real-GitHub lane, and Linux sandbox CI.
- [ ] Commit: `ops: add readiness and recovery gates`.

---

## Deletion and replacement ledger

Delete only at named migration gates:

- `team_propose_batch`, aliases, prompt text, ToolSpec, and model-side batch tests → Task 2.
- `room_lock(team_id)` → replace with thread locking in Task 6, then durable queue in Task 9.
- Fixed GroupRoom/PrivateThread architecture → compatibility redirects after Task 7; canonical thread route owns rendering.
- `thread_type`/`thread_owner_id` identity → Task 8 after backfill and client cutover.
- Detached `ConsentInbox` → Task 12 after orphan pending-action audit/migration.
- Consent batching and `batch_id` → Task 12 after pending batch count reaches zero.
- AI task proposal/update tools and duplicate task UI → Task 14 after task-to-work-thread migration.
- Shared writable repository checkout for agent work → Task 15.
- Planned-but-unenforced dependency egress constant → enforced proxy policy in Task 16.
- Production `Role.ADMIN` queue/sweep access → Task 23.

Do not delete:

- Exact-action hash, requester ownership, executor role, CAS execution, expiry, rejection reason, audit trail.
- Cited/versioned team wiki.
- Human-readable work board.
- Existing deterministic RLS/integration/E2E tests.
- Datamarking boundary.

## Phase gates

After every phase:

1. Run focused tests during each task.
2. Run `scripts/gates.sh --with-reset`.
3. Dispatch task-level reviewer for each task diff.
4. Dispatch one phase-level security/correctness reviewer.
5. Run `scripts/gates.sh --with-agent-eval` after Phase 0 baseline exists.
6. Run `pytest -m realgithub` after repository/workspace/PR changes.
7. Record migrations, backward-compatibility window, rollback method, eval delta, token/cost delta, latency delta, and known ceiling.
8. Stop if security invariant, RLS isolation, scenario truthfulness, or eval floor regresses.

## Final acceptance criteria

- Two members can run Comrade concurrently in different threads without blocking or sharing workspace state.
- Messages in one thread never enter another thread's history unless explicitly promoted into accessible memory.
- One thread has ordered, durable, cancellable, steerable, restart-safe runs.
- Public, selected-member, and private experiences map to tested RLS behavior with no metadata leak.
- Team/Comrade composer mode removes repeated mentions while remaining visually unambiguous.
- Risky tool permission appears inline, exact requester approves, same logical run continues, and no detached inbox is required.
- Plan is optional and selected by agent only when useful.
- Board shows accessible work threads and never leaks restricted/private work.
- Each work thread owns isolated repository state and can run finite commands plus supervised preview servers safely.
- GitHub CI completion returns to originating thread through signed, idempotent webhook handling and can continue failed work without polling.
- Attachments have explicit thread visibility and are not silently promoted to team memory.
- Team wiki, thread state, personal notebook, episodic runs, and procedural skills have distinct scopes/write rules.
- Skills use slash commands; MCP/connectors share one capability registry and permission boundary; arbitrary plugin code never runs in core process.
- Turn/token quotas are atomic; production workers no longer use table-owner ADMIN.
- Release report covers outcome, trajectory, memory, tools, security, variance, cost, latency, context attribution, and environment version.
