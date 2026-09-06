# Comrade production readiness: master implementation plan

**Status:** Planning complete; implementation paused at the user's request.
**Date:** 2026-09-06
**Branch:** `codex/production-boundaries`
**Goal:** Make Comrade safe for multiple untrusted teams and dependable for daily
engineering work, then finish the useful multiplayer features without rebuilding
working foundations or introducing speculative infrastructure.

**Sources:**
- September 6 conversation audit: 17 numbered findings plus collaboration UX.
- Follow-up comparison: seven additional gaps and recommendations.
- `D:/Downloads/20260904productionmultiplayerharness.md`: all 25 original tasks.
- Current source, migrations, deployment files, and tests inspected in this task.

The original document references a separate September 3 findings specification
that was not supplied. This plan does not assume its contents. The supplied
document is design input, not an instruction to execute its resets, commits, or
delegation commands. This plan supersedes the earlier first-batch plan.

## 1. Scope, current state, and implementation rules

### Already present; verify and extend instead of recreating

Canonical threads and participant RLS; thread-scoped history; durable agent runs
and steps; lease claiming; steering; effect records; backend cancellation helper;
inline consent and permission grants; optional versioned plans; thread worktrees;
post-edit PR verification gate; atomic usage reservations; control-plane role;
deterministic scenario scorer; finite Docker commands and initial previews.

These are component implementations, not a claim that their end-to-end journeys
or production configuration meet the acceptance criteria below.

### Work that existed when implementation was paused

- Branch created; no production deployment or push performed.
- Initial first-batch planning document created.
- `tests/test_deploy_host_script.py` has uncommitted regression-test edits from
  the interrupted deployment subtask. Preserve and review them in T01/T05; their
  new assertions have not been verified in this session.
- Baseline `uv run pytest tests/test_previews.py tests/test_sandbox_processes.py -q`
  completed: 42 passed; one short-HMAC-test-key warning. This does not prove
  browser-origin safety, Linux deployment correctness, or cross-container isolation.
- Two `comrade-run-*` containers were already running during inspection. Do not
  kill them without identifying ownership; leak checks must compare before/after.

### Non-negotiable constraints

1. Read current callers and tests before modifying shared behavior. A source
   finding needs a reproducer, not confidence derived from comments.
2. Use existing Python, Postgres, React, Supabase, Docker, Git, and test tools.
   Add dependencies only when the required behavior cannot reasonably use them.
3. Write the smallest regression that fails for the reported defect, observe
   that failure, implement, rerun, and review. Do not weaken assertions to pass.
4. No model-controlled identity, permission owner, run identity, or host paths.
   Tools receive server-bound team/requester/thread/workspace context.
5. RLS remains the browser authorization boundary. Migrations include matching
   grants, immutable provenance, and cross-team/restricted-thread tests.
6. Keep model/network calls outside database transactions. Use short claims and
   explicit reconciliation when external side effects cannot be transactional.
7. Preserve user files, pending consents, thread history, and dirty worktrees.
   Do not delete old representations until migration and retention checks pass.
8. Sandbox code receives no platform secrets or Docker socket. Executable
   extensions never load into credential-bearing application processes.
9. No merge, push, deployment, live external write, or destructive database reset
   is part of merely implementing this plan. Make those operations separately
   reviewable and follow the user's authorization at execution time.
10. Use expand/contract migrations with CLI-generated filenames. Names below are
    migration purposes, not invented versions to stamp into production history.
11. Stop background workers before shared database tests. Run destructive reset
    gates only against a confirmed disposable test project, then restore roles.
12. Distinguish source-verified fixes, mocked-boundary tests, live local checks,
    and production checks in the ledger. Missing infrastructure is not a pass.

### Work ledger

Keep this plan's checkboxes and a short completion record per task: changed files,
failing regression, passing checks, review findings, migration/rollback impact,
remaining ceiling. Use one branch or an isolated worktree per independently
reviewable slice. Do not change the current workspace while planning.

## 2. Architecture decisions

- **Preview isolation:** dedicated preview browser origins, per-process network
  isolation, authenticated routing, and defense-in-depth response restrictions.
  A same-origin reverse proxy with stripped headers is not sufficient. A CSP
  sandbox can provide temporary containment, but must not be reported as complete
  preview functionality when storage, assets, navigation, or WebSockets break.
- **Execution:** retain durable Postgres queues. Make Docker ownership explicit,
  add bounded concurrency and fair scheduling before considering another queue.
- **Conversation:** a thread is the unit of visibility and ordered execution.
  Browser state follows durable run identity, not the lifetime of one fetch.
- **Work tracking:** link existing tasks to execution threads first. Do not force
  every ordinary human task into a chat thread or delete task tooling prematurely.
- **Memory:** begin with verified team facts and compact thread working state.
  Personal notebooks and executable integrations are later optional phases.
- **Release:** build immutable candidates, apply compatible migrations before
  activation, serialize releases, and gate on actual deployment behavior.

## 3. Delivery order and gates

| Phase | Tasks | Exit condition |
|---|---|---|
| A: reliable baseline | T01 | Reproducers and safe test environment established |
| B: containment and deployment | T02-T08 | Preview boundaries isolated; execution and release fail safely |
| C: daily conversation reliability | T09-T15 | History, reconnect, cancellation, scheduling, and collaboration work |
| D: evidence and memory | T16-T21 | Captured knowledge is scoped, supported, recoverable, and bounded |
| E: engineering workflow | T22-T25 | Work, attachments, verification, and CI form a complete journey |
| F: operations and quality | T26-T29 | Measured recovery, security, quality, cost, and capacity gates |
| G: optional expansion | T30-T32 | Opt-in notebooks and external capabilities meet existing boundaries |

Phase G is planned, not a prerequisite to claim that the core product is ready.
Do not let optional expansion delay security fixes or silently disappear from the
backlog. T26 operational instrumentation begins alongside Phase B and is completed
before public rollout. Independent tasks may run in parallel only with disjoint
file/test-state ownership and an integration review.

## Phase A — Baseline

### T01 — Establish trustworthy baseline and executable regression coverage

**Priority/dependencies:** P0; first.
**Files:** `tests/test_deploy_host_script.py`, `tests/test_previews.py`,
`tests/test_sandbox_processes.py`, `scripts/gates.sh`, `frontend/tsconfig.json`,
`frontend/package.json`; add focused browser/network test files as required.

- [ ] Review the interrupted test diff before using it. Ensure fake commands
      cannot invoke deployment operations or operate outside temporary directories.
- [ ] Inventory current API/worker processes, Docker containers, schema versions,
      branch state, and installed runtimes without exposing `.env` values.
- [ ] Replace the ineffective frontend solution-config typecheck with the actual
      `npm run build`; include `npm run lint` in the release gate.
- [ ] Record baseline lane failures separately from new failing regressions.
- [ ] Retain the deterministic team-scenario scorer and test actual rows/effects,
      never assistant claims. Keep model-side batch proposal removal intact.

**Checks:** run focused baseline tests above, frontend build/lint, then the normal
gate when local infrastructure is confirmed. A deliberately broken referenced TS
fixture must fail the chosen build command. Restore that fixture afterward.
**Done:** failures are attributable; no green lane that fails to compile the app.

## Phase B — Containment and deployment

### T02 — Isolate preview browser origins and authentication

**Priority/dependencies:** P0; T01; ships with T03/T04 before previews reopen broadly.
**Files:** `server/previews.py`, preview routes in `server/app.py`,
`shared/config.py`, `docker/Caddyfile`, Compose configuration,
`frontend/src/components/PreviewBar.tsx`, `.env.example`, `docs/deployment.md`.

**Design:** each process gets a distinct origin under an operator-controlled
preview domain separate from Comrade's application domain. Bind the hostname to
the database process identity. Exchange a short-lived, single-use launch grant
for a host-only, Secure, HttpOnly preview session; never share app cookies or
Supabase tokens. Recheck current participant access on each HTTP request and
WebSocket connection. No wildcard cookie Domain. A missing/unsafe preview-domain
configuration fails closed with an actionable status.

- [ ] Reproduce same-origin localStorage access in a real browser using a harmless
      sentinel, not real account credentials.
- [ ] Add origin/domain validation, grant replay/expiry tests, hostname/process
      mismatch denial, and token-audience separation.
- [ ] Strip upstream Set-Cookie and security-header overrides unless an explicitly
      isolated preview cookie policy is implemented; set no-store and no-referrer.
- [ ] Provide proxy configuration and DNS/TLS setup instructions. Avoid unrestricted
      on-demand certificate issuance; validate allowed process hosts.
- [ ] If full origin configuration cannot be activated locally, keep previews
      disabled there or explicitly contained; do not imply production is fixed.

**Checks:** browser sentinel remains inaccessible, sibling preview session cannot
be used, app APIs refuse preview credentials, removed participant loses access.
**Rollback:** disable preview access; never restore unsafe same-origin execution.

### T03 — Isolate preview networks and enforce setup egress

**Priority/dependencies:** P0; T01; coordinates with T02/T05.
**Files:** `agent/processes.py`, `agent/sandbox.py`, Compose files,
`pipeline/repo_deps.py`, `tests/test_sandbox_processes.py`, `tests/test_repo_deps.py`;
new Linux network integration test and proxy configuration if required.

**Design:** one isolated network per preview process or equally strong enforced
network policy. Only the authenticated proxy can reach the declared port. Preview
containers cannot reach sibling sandboxes, platform service ports, host services,
metadata endpoints, or external destinations. Removing the shared bridge is
necessary but not sufficient: test reverse traffic toward the proxy as well.

- [ ] Replace the single shared `comrade-preview` topology. No host-published ports.
- [ ] Attach/detach proxy endpoints using server-owned identifiers; verify actual
      network configuration instead of trusting the existence of a network name.
- [ ] Non-preview commands remain network-none. Enforce registry egress for setup;
      block direct-IP, DNS, IPv6, redirect, and metadata bypasses.
- [ ] Use an internal registry proxy/firewall policy; remove the unused planned
      allowlist only when its replacement is active and tested.
- [ ] Fail closed on policy setup failure. Record and reclaim network ownership.

**Checks:** two real containers on a Linux daemon cannot connect across teams or
back into platform services; approved package retrieval works; forbidden egress
fails. Unit argv assertions are supplemental, never the only isolation evidence.
**Rollback:** disable new starts; drain old shared-network previews before rollout.

### T04 — Complete preview HTTP and browser behavior

**Priority/dependencies:** P1; T02/T03.
**Files:** preview routes/helpers, `PreviewBar.tsx`, browser preview tests.

- [ ] Serve application root paths on each preview origin so absolute scripts,
      relative assets, forms, redirects, and client-side navigation work naturally.
- [ ] Forward query parameters using encoded multi-value pairs, preserving literal
      `&`, `+`, `=`, Unicode, and repeated keys.
- [ ] Bound request and response bytes while streaming; reject overflow instead of
      slicing a fully buffered response or silently returning corrupted content.
- [ ] Handle decompression/content-encoding consistently; sanitize hop-by-hop headers.
- [ ] Implement WebSocket proxying with authorization, connection lifetime limits,
      and cleanup. Use installed support where viable; document any needed library.
- [ ] Open a browser tab synchronously on click, then navigate after grant creation
      to avoid popup blocking. Show startup, expired, stopped, and failed states.

**Checks:** real small app with JS/CSS, navigation, API call, redirect, and HMR;
oversized/chunked responses; Unicode query; expired token; unavailable upstream.
**Done:** initial HTML loading alone is insufficient evidence.

### T05 — Repair packaged Docker execution and release ordering

**Priority/dependencies:** P0; T01; coordinate preview topology with T03.
**Files:** `docker/app.Dockerfile`, both Compose files, `scripts/deploy_host.sh`,
`.github/workflows/deploy-pilot.yml`, `tests/test_deploy_host_script.py`, deployment docs.

- [ ] Install a pinned/controlled Docker CLI in execution images. Give Docker-owning
      workers the host socket group through explicit deployment configuration.
- [ ] Ensure dependency setup, volume maintenance, process reaping, and agent command
      execution run in services that actually have daemon access. Keep it off API
      and untrusted containers; share an image only when privilege stays explicit.
- [ ] Validate host bind paths, non-root ownership, socket access, and sandbox image
      availability. Do not rely on image-layer ownership for host-mounted folders.
- [ ] Release order: acquire host lock, fetch exact SHA, build candidate images,
      run one-off migrations, activate services, then check readiness/smoke behavior.
- [ ] Add workflow concurrency and host locking. Do not cancel an in-flight migration
      because a newer push arrives. Require build/test prerequisites for deployment.
- [ ] Retain previous immutable image identity for rollback. A failed build or
      migration must not replace the healthy stack.

**Checks:** run shell with fake external commands for build failure, migration
failure, lock contention, successful ordering, and readiness failure; then build
and smoke actual Compose services against a disposable stack on Linux.
**Rollback:** code rollback only if schema remains backward compatible; never
automatically reverse a destructive migration.

### T06 — Make process lifecycle truthful and restart-safe

**Priority/dependencies:** P0; T03/T05.
**Files:** `agent/processes.py`, `pipeline/worker.py`, process migration if needed,
`tests/test_sandbox_processes.py`, `frontend/src/components/PreviewBar.tsx`.

- [ ] Persist intended container/network identity before Docker launch. Recover a
      crash before ID write-back by inspecting that persisted identity.
- [ ] Reconcile actual running/exited/missing states, exit code, and retained logs.
- [ ] Check kill/remove exit codes. Mark stopped only when removal is confirmed or
      the container is authoritatively absent. Keep cleanup failures retryable.
- [ ] Handle cancellation during startup and DB failure after Docker launch without
      creating anonymous resources. Clean failed-start networks too.
- [ ] Distinguish idle lifetime from absolute lifetime; update activity on accepted
      use. Cap lifetime even if requests keep a process busy indefinitely.
- [ ] Preserve ownership evidence across thread/team deletion until cleanup finishes.

**Checks:** terminate at each launch boundary; simulated daemon failure; repeated
stop; early process exit; active preview; expired preview; deleted-owner cleanup.
**Done:** DB status reflects Docker state and no new orphan remains after recovery.

### T07 — Bound sandbox output, disk, and aggregate resource use

**Priority/dependencies:** P0; T05/T06.
**Files:** `agent/sandbox.py`, `agent/processes.py`, `pipeline/repo_sync.py`,
`pipeline/repo_env.py`, settings, sandbox tests.

- [ ] Replace unbounded `capture_output` on untrusted commands with incremental
      bounded capture retaining useful head/tail output and a truncation signal.
- [ ] Drain stdout/stderr concurrently to avoid deadlock; kill the container on
      timeout/cancellation/output-limit policy, not merely the Docker client.
- [ ] Cap detached logs and writable scratch/workspace storage. Periodic eviction
      alone does not prevent a disk-filling command.
- [ ] Add per-team and host-wide active-process/resource admission limits.
- [ ] Eviction must preserve dirty worktrees and refuse active workspace deletion.
      Report measurement failures rather than interpreting them as zero usage.

**Checks:** noisy stdout/stderr, large single line, fork/CPU pressure, disk fill,
quota contention, dirty workspace eviction refusal, no leaked containers.
**Done:** malicious output cannot grow application memory without bound.

### T08 — Make dependency environments reproducible and consistent

**Priority/dependencies:** P1; T03/T05/T07.
**Files:** `pipeline/repo_deps.py`, `pipeline/repo_env.py`, `agent/sandbox.py`,
`agent/processes.py`, `docker/sandbox.Dockerfile`, environment tests.

- [ ] Consume the selected lockfile with its matching frozen installer. Merely
      finding/hashing `uv.lock` or `poetry.lock` must not satisfy reproducibility.
- [ ] Define supported manifests/runtimes explicitly. Support the pilot's Python
      and JavaScript projects; show unsupported Go/Rust/etc. before attempting runs.
- [ ] Include lock, recipe, runtime/image, and relevant source identity in cache keys.
- [ ] Mount the same read-only dependencies into finite commands and preview servers.
- [ ] Verify tests import the edited workspace rather than a stale installed package.
- [ ] Build replacement environments separately, activate atomically, and retain
      clear disabled/building/ready/stale/failed status with a retry action.

**Checks:** frozen reinstall reproducibility, changed lock/source, failed setup,
preview import, src-layout edited-code test, no Comrade credentials in environment.

## Phase C — Conversation and agent reliability

### T09 — Paginate messages without hiding new conversation

**Priority/dependencies:** P1; T01; can follow containment work independently.
**Files:** `frontend/src/hooks/useMessages.ts`, `GroupRoom.tsx`, component and
integration message tests; reuse existing thread/created_at/id index.

**Contract:** `messages`, `loading`, `loadingOlder`, `hasOlder`, `loadOlder()`,
`refresh()`, and explicit error state. Fetch newest page descending by
`(created_at,id)`, then display chronologically. Use keyset older-page cursors.

- [ ] Add a 501+ message test proving the latest message appears and older messages
      remain reachable. Include identical timestamps to prevent gaps/duplicates.
- [ ] Merge pages by ID; refresh recent records without dropping loaded history.
- [ ] Keep scroll position when prepending history. Follow new messages only near
      the bottom; otherwise show an unread/new-message affordance.
- [ ] Fetch only compilation cards referenced by loaded messages and report errors.
- [ ] Discard responses for a superseded thread/team request.

**Checks:** component + real Supabase integration; incoming/deleted messages during
pagination, slow old-thread response, scroll anchor retention.

### T10 — Reconnect browser to durable runs and prevent duplicate sends

**Priority/dependencies:** P1; T09; existing queue retained.
**Files:** `frontend/src/lib/agentApi.ts`, `GroupRoom.tsx`, `server/app.py`,
`agent/run_queue.py`, run migration for idempotency if needed.

- [ ] Generate a client request ID per send; retain it across uncertain retries.
      Uniqueness binds requester/team/thread/request ID to one accepted message/run.
- [ ] Persist accepted run ID in frontend state and reconstruct active runs from
      server history after refresh. Attach to existing GET stream rather than POST.
- [ ] Accept an event cursor and replay only later sequence numbers; deduplicate.
- [ ] Detect truncated streams and distinguish failure-before-acceptance from
      accepted-but-disconnected. Do not present an accepted draft as unsent.
- [ ] Separate lifecycle state from text/tool events, including waiting states.
- [ ] Cancel subscriptions on navigation without cancelling server work implicitly.

**Checks:** disconnect before/after admission, duplicate POST, refresh mid-tool,
out-of-order frame, reconnection after completion, no duplicate effects/messages.

### T11 — Expose cancellation, steering, and permission continuation

**Priority/dependencies:** P1; T10/T06.
**Files:** `server/app.py`, `agent/run_queue.py`, `agent/runtime.py`,
`agent/effects.py`, `agent/permission_plugin.py`, `GroupRoom.tsx`, `ConsentCard.tsx`.

- [ ] Expose authenticated cancellation using thread access and explicit requester
      ownership; participants may cancel their own requests, not impersonate others.
- [ ] Propagate cancellation to running subprocess/container work where possible;
      stop further model/tool calls, settle usage, and surface final state.
- [ ] Show steering messages with sender attribution and deliver before the next
      model call. Preserve server identity and immutable original approval ownership.
- [ ] After approve/edit/reject/expiry, resume the same logical run and reconnect UI.
- [ ] Recheck reusable grants on every effect; preserve scope, expiry, risk ceiling,
      and requester-only resolution. Publication/destructive actions stay allow-once.
- [ ] Reconcile uncertain external effects rather than automatically repeating them.

**Checks:** cancellation in queue/model/tool/permission states; revoked access;
worker restart after effect; rejection feedback; duplicate approval; resource mismatch.

### T12 — Add fair bounded worker concurrency and independent maintenance

**Priority/dependencies:** P1; T05/T07/T11.
**Files:** `agent/worker.py`, `agent/run_queue.py`, `pipeline/worker.py`, settings,
Compose worker configuration, queue/concurrency tests.

- [ ] Allow a configured bounded number of independent thread turns concurrently,
      with a per-team ceiling and one active ordered run per thread.
- [ ] Keep queue ownership fenced by worker/lease token. Stop effects on lease loss;
      a stale worker must not finish another worker's reclaimed job.
- [ ] Replace drain-until-empty maintenance scheduling with bounded drain batches
      and independent periodic sweeps/reaping.
- [ ] Add retry backoff and clear terminal failure/retry state for pipeline jobs.
- [ ] Renew long-job leases; make pipeline completion conditional on claim ownership.
- [ ] On shutdown, stop new claims, drain within limits, then release/recover work.

**Checks:** two teams progress concurrently; same-thread ordering; hot-team fairness;
maintenance under continuous ingestion; expired lease race; shutdown while busy.

### T13 — Make streaming, realtime, and pools scale predictably

**Priority/dependencies:** P1; T10/T12.
**Files:** `shared/agent_runs.py`, stream routes, `shared/db.py`,
`frontend/src/hooks/useRealtime.ts`, list hooks, load tests.

- [ ] Query agent steps after cursor instead of fetching all steps every 200 ms.
- [ ] Use bounded adaptive polling or existing event mechanisms; avoid a new broker
      until measurements require one. Keep heartbeat and disconnect semantics.
- [ ] Reconcile realtime deltas or coalesce invalidations; do not refetch every
      team-wide compilation/list on every message event.
- [ ] Surface connection loss and refetch on successful resubscribe as well as focus.
- [ ] Synchronize lazy pool initialization, bound total connections per service,
      and keep blocking DB calls off async event-loop paths.

**Checks:** reconnect gap, event burst, concurrent first requests, large run log;
measure DB queries/bytes per viewer and p95 API latency under representative load.

### T14 — Finish thread visibility and participant management UX

**Priority/dependencies:** P1; T09/T11.
**Files:** `useThreads.ts`, `Threads.tsx`, `GroupRoom.tsx`, `TeamContext.tsx`,
`Sidebar.tsx`, participant RLS/audit migrations if needed.

- [ ] Offer team-visible and restricted creation, participant selection, owner
      management, and labels Private versus Selected members.
- [ ] Warn clearly that adding a participant shares full existing thread history;
      show the change to existing participants and audit the actor/time.
- [ ] Removal immediately revokes history, runs, approvals, plans, attachments,
      previews, and board metadata access, including active subscriptions.
- [ ] Allow ordinary team discussion in public discussion threads, not only General.
      Default work threads to Agent while preserving an explicit mode switch.
- [ ] Realtime-update thread lists, titles, roster, and access changes; preserve
      good state during transient request failure rather than showing empty data.

**Checks:** two-browser participant invite/remove journey, cross-team denial,
private metadata absence, composer persistence, focus/keyboard/mobile controls.

### T15 — Consolidate daily UX failure and recovery states

**Priority/dependencies:** P1; T09-T14.
**Files:** `GroupRoom.tsx`, `Documents.tsx`, `TeamContext.tsx`, `agentApi.ts`,
shared components only where an existing pattern can be reused.

- [ ] Check delete/update/open-tracking results; never imply a failed action worked.
- [ ] Preserve drafts per thread and during unrelated requests; prevent double sends.
- [ ] Distinguish loading, confirmed empty, stale, denied, offline, and failed states.
- [ ] Replace developer-facing ':8000 running?' errors with user recovery actions
      plus a correlation ID; keep technical detail in logs.
- [ ] Add actionable retry for failed ingestion and startup, upload progress, and
      accessible inline status. Do not force users to reupload an existing file.
- [ ] Test long command/message content, keyboard navigation, focus after errors,
      narrow layout, and screen-reader status announcements.

**Checks:** failure-focused component tests and a mobile/keyboard browser journey.

## Phase D — Evidence and memory

### T16 — Make capture timely, bounded, and recoverable

**Priority/dependencies:** P1; T12.
**Files:** `pipeline/chat.py`, `pipeline/compiler.py`, `pipeline/github.py`,
capture-watermark migration if needed, chat/compiler tests.

- [ ] Trigger capture by message count OR maximum age so quiet decisions are saved.
- [ ] Bound each batch by records and estimated tokens/bytes; preserve per-thread
      context rather than ambiguously interleaving unrelated public conversations.
- [ ] Use stable timestamp/ID boundaries or durable source acknowledgements; do not
      lose equal-timestamp records or records committed after a watermark snapshot.
- [ ] Treat malformed/absent model output as retryable failure, distinct from a
      valid empty result. Advance source progress only after a successful apply.
- [ ] Deduplicate overlapping scheduled/on-demand jobs and allow visible retry.

**Checks:** one message ages into capture; backlog spans batches; malformed output
does not advance; equal timestamps/late commit; crash after apply before job finish.

### T17 — Separate requests to Comrade from team decisions

**Priority/dependencies:** P1; T16.
**Files:** `pipeline/chat.py`, `pipeline/compiler.py`, `evaluation/team_scenario.py`,
`evaluation/extraction_set.py`, extraction/chat-memory tests.

- [ ] Carry source context indicating agent-directed input, thread, author, and
      explicit remember/promotion intent into extraction.
- [ ] Do not convert questions, proposed options, or requests to investigate into
      settled team knowledge. Preserve explicit decisions even when addressed to AI.
- [ ] Tighten scorer logic: agent-directed provenance alone is not proof that a
      genuine decision is invalid; use labeled examples and evidence semantics.
- [ ] Include “investigate switching DB” versus “we decided to switch DB; implement
      it,” tentative assignments, negation, corrections, and quoted statements.

**Checks:** deterministic labeled fixtures plus repeated live extraction evaluation;
report precision/recall, not just duplicate counts. No private promotion by default.

### T18 — Verify citations and fail safely on uncertain consolidation

**Priority/dependencies:** P1; T16/T17.
**Files:** `pipeline/compiler.py`, `pipeline/chat.py`, `pipeline/wiki.py`,
memory citation/trust migration, `Wiki.tsx`, compiler/citation tests.

- [ ] Resolve source IDs under team/thread scope and validate excerpts against the
      actual normalized source. A generated quote must not become evidence by itself.
- [ ] Keep unsupported candidates proposed/quarantined; do not publish them as facts.
- [ ] Replace malformed decision-to-add fallback with retry or explicit rejection.
- [ ] Bind revisions to expected current version; serialize apply conflicts or retry
      consolidation from fresh state to prevent stale concurrent overwrites.
- [ ] Track observed/proposed/confirmed/verified/superseded trust without claiming
      that textual entailment can be fully established by substring matching.
- [ ] Explain source, date, trust, and supersession in the wiki; preserve revert/audit.

**Checks:** fabricated excerpt, wrong source index/team, missing source, stale
revision, duplicate apply, contradictory facts, member correction/revert.

### T19 — Add compact durable thread working memory

**Priority/dependencies:** P1; T11/T18.
**Files:** `agent/history.py`, `agent/runtime.py`, `agent/plan_tools.py`,
thread-working-state migration, `GroupRoom.tsx`, history/resume tests.

**Contract:** thread-scoped rolling summary, summary-through cursor, pinned
constraints, open questions, plan version, workspace revision, and pending state.
Structured identity/approval/diff records remain authoritative outside the summary.

- [ ] Keep recent messages plus bounded summary; compact only an acknowledged range.
- [ ] Preserve older constraints and unresolved questions without paraphrasing exact
      approved arguments, permission ownership, paths, or current diff into authority.
- [ ] Allow users to inspect/correct pins and summaries under thread RLS.
- [ ] Rehydrate from durable state after refresh/restart and datamark summary content.
- [ ] Do not auto-create plans or treat plan completion as work completion.

**Checks:** important constraint survives 100+ messages; correction supersedes old
summary; concurrent compaction conflict; private state never appears in team wiki.

### T20 — Bound retrieval cost and measure memory usefulness

**Priority/dependencies:** P1; T18/T19.
**Files:** `agent/agent.py`, `agent/tools.py`, `pipeline/wiki.py`,
`pipeline/compiler.py`, retrieval/evaluation tests.

- [ ] Read page metadata for the index without loading every fact first.
- [ ] Apply token/byte budgets to index, history, retrieved pages, and observations.
- [ ] Retrieve candidate pages broadly with existing FTS, rerank cheaply, then load
      only relevant facts/citations. Do not add embeddings without measured need.
- [ ] Prevent one oversized page from bypassing the consolidation budget; split or
      retrieve facts with enough context to safely identify revisions.
- [ ] Attribute tokens to prompt, tool schema, history, memory, files, and output.

**Checks:** growing corpus benchmark, paraphrase/negation/identifier cases, revision
recall beyond 400 facts, capped payload sizes, no silent unknown-to-add fallback.

### T21 — Minimize ingestion data and align deletion/retention behavior

**Priority/dependencies:** P1; T16/T18; coordinates T24/T26.
**Files:** `Documents.tsx`, `server/app.py`, `pipeline/compiler.py`, job/storage
schema and policies, `shared/db.py`, deletion/export tests.

- [ ] Upload bytes once to private Storage; queue authorized storage references and
      content identity, not complete base64 documents readable by the control role.
- [ ] Fetch/parse under the narrow worker permission and enforce size/type limits
      before allocating or parsing large bodies. Reject `.doc` unless supported.
- [ ] Persist failure reason and retry state; clean orphan uploads after a grace
      period without deleting a concurrent successful upload.
- [ ] Recheck deletion/access before parse and before apply. Define which derived
      facts are retracted/redacted when a source is deleted; avoid silent resurrection.
- [ ] Apply retention to raw payloads, private steps, logs, artifacts, and exports
      while preserving necessary consent audit metadata.

**Checks:** storage succeeds/DB fails, enqueue fails/retry, source deleted mid-job,
unsupported binary, oversized file, export scope, retention dry-run and purge.

## Phase E — Engineering workflow

### T22 — Connect tasks, work threads, and optional plans

**Priority/dependencies:** P2; T14/T19.
**Files:** `Tasks.tsx`, `useTasks.ts`, `Threads.tsx`, thread/task link migration,
`agent/tools.py`, `plan_tools.py`, board/RLS tests.

- [ ] Link an existing task to a work thread when execution context is needed;
      allow ordinary human tasks without a thread and work threads without a plan.
- [ ] Establish one authoritative work status; derive board cards showing owner,
      due date, current run, blockers, PR/CI, last activity, and plan progress.
- [ ] Keep restricted work invisible to non-participants, including counts/previews.
- [ ] Never auto-close work from plan completion or assistant prose. Require the
      explicit human action or recorded completion criterion.
- [ ] Migrate historical links with preserved IDs/export references. Remove duplicate
      task tools/UI only after replacement coverage and no remaining callers.

**Checks:** ordinary task, linked work, no-plan work, conflict updates, restricted
card denial, existing-task migration and rollback. Full task-to-thread replacement
from original Task 14 is intentionally replaced by this smaller linked model.

### T23 — Tie PR verification to the actual proposed workspace revision

**Priority/dependencies:** P1; T08/T11.
**Files:** `agent/repo_tools.py`, `agent/runtime.py`, `shared/workspace.py`,
`pipeline/repo_pr.py`, verification/workspace/real-GitHub tests.

- [ ] Keep the existing latest-edit gate, but bind checks to workspace content or
      patch identity rather than only in-memory edit counters.
- [ ] Invalidate verification after any mutation path, including commands and
      background processes. Detect changes between check completion and PR capture.
- [ ] Record the actual relevant check, exit status, revision, and limitations.
      `python -c 'pass'` must not satisfy a meaningful project verification contract.
- [ ] Require project-declared checks or explicit reviewed verification policy;
      distinguish unavailable environment from failing project code.
- [ ] Verify per-thread workspace isolation, active/dirty cleanup protection,
      base-refresh conflicts, and GitHub branch/patch idempotency.

**Checks:** edit/check/edit/propose; command mutation; stale installed package;
restart; simultaneous threads; real GitHub PR against the verified patch when
authorized. Do not delete pending legacy consent/batch data without an orphan audit.

### T24 — Add scoped thread attachments and explicit knowledge promotion

**Priority/dependencies:** P1; T14/T21.
**Files:** thread composer, `Documents.tsx`, `agentApi.ts`, ingestion routes,
attachment purpose/storage policies migration, parser/attachment/browser tests.

**Contract:** `turn_context | thread_artifact | team_knowledge`; default turn context.
Attachment rows bind document/message/thread/team through validated relationships.

- [ ] Reuse single-upload pipeline; authorize binary Storage access at the same
      scope as metadata, not just the frontend or download API.
- [ ] Provide progress, cancel/retry, original download, parse failure, and citation.
- [ ] Keep restricted attachments out of public search/compiler/export by default.
- [ ] Require explicit member promotion with a visibility warning and source audit.
- [ ] Bound content supplied to model; retain full original behind scoped read tool.

**Checks:** two-thread and cross-team denial, revoked participant, signed URL
expiry policy, private file not compiled, explicit promotion, deletion while queued.

### T25 — Return GitHub CI results to originating work

**Priority/dependencies:** P1; T22/T23/T12.
**Files:** `server/webhooks.py`, `server/app.py`, `pipeline/github.py`,
`shared/github_app.py`, run queue, thread UI, CI correlation migration/tests.

- [ ] Persist signed delivery and repository/PR/head-SHA/thread correlation before
      acknowledgement; deduplicate deliveries and out-of-order completion events.
- [ ] Handle check-run/check-suite/workflow terminal states and show check links,
      conclusion, commit, and clipped datamarked failure summaries.
- [ ] Ignore stale-head failures for continuation decisions; preserve them in audit.
- [ ] Permit at most one policy-authorized continuation per failure revision under
      existing quota and permissions. Never automatically publish a new patch.
- [ ] Fetch logs only through server-held App credentials; redact secrets.

**Checks:** signature failure, duplicate, unknown repo, old SHA, success/failure/
cancelled, cross-team mapping, continuation budget failure, authorized real CI journey.

## Phase F — Operations and quality

### T26 — Tighten service secrets, roles, and admission budgets

**Priority/dependencies:** P0/P1; start with T05, finish after T12/T21.
**Files:** settings, `shared/db.py`, `shared/usage.py`, service env configuration,
role scripts/migrations, API/compiler/runtime budget paths.

- [ ] Provide only necessary credentials to each service. Move table-owner migration
      URL into a one-off migration service; remove it as a required runtime setting.
- [ ] Keep control-plane role unable to read document bodies, prompts, or consent
      arguments indirectly through queues. Audit grants and SECURITY DEFINER access.
- [ ] Preserve atomic admission and idempotent reconciliation; handle crash between
      reservation, enqueue, and reservation linkage without leaking quota.
- [ ] Add in-run model budget checks and compilation/setup quotas; a 6,000-token
      estimate is admission accounting, not a hard ceiling for a 20-call turn.
- [ ] Apply per-team limits to uploads, jobs, previews, and external calls. Report
      retry timing and consumed/reserved usage without leaking another team's state.
- [ ] Test role/key rotation and fail-closed missing configuration.

**Checks:** cross-role denial, concurrent admission, all terminal paths, lease loss,
duplicate finalization, expensive pipeline batch, missing usage metadata reporting.

### T27 — Add truthful health, structured observability, and safe drain

**Priority/dependencies:** P1; T05/T06/T12/T26.
**Files:** health/readiness routes, both workers, shared DB/logging helpers,
settings, `docs/operations.md`, readiness/drain/log-redaction tests.

- [ ] Separate process liveness from dependency readiness and document orchestration
      behavior. Docker Compose health status alone does not restart containers.
- [ ] Check the complete required migration set, role connectivity, worker heartbeat
      freshness, expired running leases, pipeline failures, and sandbox readiness.
- [ ] Emit structured correlation fields for request/run/job/thread; never raw
      credentials, query tokens, private prompts, or attachment bodies.
- [ ] Track queue age, lease loss, tool errors, permission wait, resource admission,
      preview failures, compiler lag, token/cost, and denial metrics.
- [ ] Define alerts and operator actions; test graceful stop avoids new claims and
      leaves interrupted work recoverable within a measured window.

**Checks:** empty queue/dead worker, missing middle migration, dead Docker, pool
failure, stalled pipeline, redaction fixtures, draining under continuous load.

### T28 — Rehearse recovery and document release operations

**Priority/dependencies:** P1; T05/T21/T27.
**Files:** `docs/deployment.md`, new `docs/operations.md`, role restoration scripts,
backup/restore smoke tooling and tests.

- [ ] Document stable domain, SMTP, GitHub App callbacks, DNS/TLS, secret rotation,
      deployment prerequisites, incident disable switches, and maintenance ownership.
- [ ] Define measurable recovery-point/recovery-time targets for DB, Storage,
      uncommitted workspaces, and audit data. Do not call dirty worktrees rebuildable.
- [ ] Restore a disposable backup including roles, policies, Storage, consent state,
      and workspace metadata; verify usable user journeys after restoration.
- [ ] Exercise prior-image rollback after compatible migration and record limits
      for destructive contracts. No automatic downgrade of irreversible schema.
- [ ] Establish retention windows, export/delete behavior, and backup access policy.

**Checks:** timed restore drill and rollback drill with evidence artifact; sample
thread/message/wiki/consent/attachment access and revoked-user denial after restore.

### T29 — Gate releases on outcomes, adversarial cases, and measured capacity

**Priority/dependencies:** P1; incremental from T01, complete after T02-T28.
**Files:** evaluation modules, new adversarial cases, `scripts/gates.sh`, CI workflows,
Linux Docker and browser journeys, sanitized evidence/report tooling.

- [ ] Preserve deterministic scenario truth checks; correct scorer blind spots and
      keep held-out cases separate from ordinary development examples.
- [ ] Add isolation, simultaneous threads, cancel/resume, permissions, previews,
      attachments, memory promotion, verification, and prompt-injection cases.
- [ ] Run at least five samples for stochastic cases before setting quality floors;
      record distributions/confidence intervals and absolute security invariants.
- [ ] Record model, prompt, capability, runtime/environment digests, outcome,
      trajectory, precision/recall, latency, tokens, and cost together.
- [ ] Run small deterministic/behavioral merge gates plus scheduled fuller model
      evaluation; do not make every local test depend on paid services.
- [ ] Load-test realistic team/message/wiki/job sizes; establish p95 latency,
      fairness, connection/memory/disk ceilings, and capacity operating limits.
- [ ] Require Linux Compose execution, multi-container isolation, and real browser
      journeys. Run real-GitHub lane for relevant changes with explicit external scope.

**Done:** release evidence proves behaviors, not the presence of flags or source
strings. A security failure blocks release regardless of average quality score.

## Phase G — Explicitly later, optional expansion

### T30 — Add opt-in personal and episodic memory access

**Priority/dependencies:** P2; T18-T21/T26/T29; not core rollout prerequisite.
**Files:** memory scope migrations, agent retrieval tools, private notebook UI,
export/deletion tests.

- [ ] Add a requester-private notebook only when enabled; no silent extraction from
      private chats into personal or team memory.
- [ ] Retrieve past run episodes through permission-scoped bounded summaries, not
      raw team-wide logs in every prompt.
- [ ] Make scope, origin, edit/delete/export, retention, and promotion explicit.
- [ ] Reuse existing wiki fact/version machinery only where scope can be enforced
      cleanly; do not build a second generic memory framework.

**Checks:** opt-in/out, two-user isolation, removed access, deleted source,
cross-scope retrieval, explicit promotion, budget bounds.

### T31 — Add scoped skills and slash-command discovery

**Priority/dependencies:** P2; T26/T29; before external connectors.
**Files:** existing `agent/registry.py`, new `agent/skills.py`, capability metadata
module/migration if necessary, slash menu, skill/registry/RLS tests.

- [ ] Extend the existing registry rather than introducing competing authorization
      registries. Track origin, owner scope, enabled scope, version/digest, declared
      permissions, install actor/time, and revocation.
- [ ] Load repository/team skills as text only; index metadata cheaply, load body
      when selected, datamark untrusted content, and preserve permission boundaries.
- [ ] Parse `/skill arguments`, resolve name collisions explicitly, and show only
      readable/enabled skills for the current user/thread.
- [ ] Recheck enablement/revocation during runs. Skill text cannot authorize tools.

**Checks:** scope denial, collision, digest change, malicious instructions,
revocation mid-run, menu keyboard access, no executable imports.

### T32 — Add isolated MCP and OAuth connectors

**Priority/dependencies:** P2; T31 and all core containment/quality gates.
**Files:** new `agent/mcp_client.py`, `server/connectors.py`, registry and permission
integration, secret-provider configuration, connector/adversarial tests.

- [ ] Start with one concrete connector demand, not a generic plugin ecosystem.
- [ ] Permit approved HTTPS endpoints; validate resolved IPs, redirect hops, DNS
      rebinding, TLS, and private/metadata destinations at connection time.
- [ ] Keep OAuth refresh tokens behind server-side opaque secret references.
- [ ] Snapshot tool schema/digest and reject poisoned/oversized results. Every call
      has timeout, output limit, audit, scoped rate limit, and failure circuit.
- [ ] Route writes through exact-action consent or valid narrow grants; revoke
      capabilities immediately. Run executable extensions outside core processes.

**Checks:** SSRF/DNS/redirect bypass, schema mutation, expired/revoked OAuth,
cross-team secret use, stalled server, oversized output, replayed approval,
malicious tool text, and partial external success reconciliation.

## 4. Coverage map: every supplied original task

| Original task | Current disposition | Master task(s) |
|---|---|---|
| 1 Fail-closed four-person scenario | Scorer exists; retain and strengthen | T01, T17, T29 |
| 2 Remove model batch proposal | Largely present; preserve, audit remnants | T01, T23 |
| 3 Team-bound consent resolution | Present; verify all resolution paths | T11, T26 |
| 4 Verification after latest edit | Present but revision/check strength needs work | T23 |
| 5 Canonical threads schema | Present; verify grants/provenance | T14, T26 |
| 6 Thread-scoped server/history/locks | Present; verify concurrency and removal | T11-T14 |
| 7 Thread UI and composer | Partial; restricted creation/mode gaps | T14, T15 |
| 8 Contract legacy message identity | Largely present; audit remaining consumers | T14, T21, T29 |
| 9 Leased run queue | Present; frontend recovery/capacity incomplete | T10-T12 |
| 10 Steering and restart continuation | Partial end-to-end | T10, T11, T19 |
| 11 Thread approvals and run pause | Present; verify resume/revocation | T11 |
| 12 Inline cards and inbox deletion | Largely present; orphan audit before removals | T11, T23 |
| 13 Optional plans | Present; preserve optionality and CAS | T19, T22 |
| 14 Replace tasks with thread board | Revised: link first, migrate only justified duplication | T22 |
| 15 Isolated workspaces | Present; dirty/active/revision recovery tests remain | T07, T23, T28 |
| 16 Sandbox/process supervision | Partial; serious containment/lifecycle gaps | T03, T05-T08 |
| 17 Authenticated previews | Partial; browser/network/transport incomplete | T02-T04, T06 |
| 18 GitHub CI return path | Missing | T25 |
| 19 Thread attachments | Missing | T21, T24 |
| 20 Memory layers and trust | Split into core fixes and optional notebook | T16-T21, T30 |
| 21 Skills and capability registry | Later feature; reuse existing registry | T31 |
| 22 MCP/OAuth extensions | Later feature; requires proven boundaries | T32 |
| 23 Quotas and control role | Present with indirect access/coverage gaps | T21, T26 |
| 24 Release evaluation | Deterministic basis present; broader gates absent | T01, T29 |
| 25 Operations and recovery | Partial readiness/docs | T05, T12, T27, T28 |

## 5. Coverage map: September 6 audit and additions

| Audit finding | Master task(s) |
|---|---|
| 1 Same-origin preview session exposure | T02 |
| 2 Shared preview network | T03 |
| 3 Docker deployment wiring | T05 |
| 4 Deploy-before-migrate / overlapping releases | T05 |
| 5 Oldest 500 messages | T09 |
| 6 Durable run / non-durable browser UX | T10, T11 |
| 7 Preview assets/auth/WebSockets | T04 |
| 8 Output/disk/egress limits | T03, T07 |
| 9 False process stop / crash orphan | T06 |
| 10 Serial workers / starved maintenance | T12 |
| 11 Lockfiles/runtime/preview environment mismatch | T08 |
| 12 Capture delay / unbounded backlog / malformed extraction | T16 |
| 13 Unsupported citations / unsafe consolidation fallback | T18 |
| 14 Long-thread forgetfulness | T19 |
| 15 Shared credentials / document-bearing queue | T21, T26 |
| 16 Misleading readiness / unrehearsed restore | T27, T28 |
| 17 Tests mirror source / ineffective typecheck | T01, T29 |
| Collaboration: agent-only public threads | T14 |
| Collaboration: upload retry / forced scroll / opaque failures | T09, T15, T21 |
| Added: request versus decision semantics | T17 |
| Added: participant selection and history sharing | T14 |
| Added: attachment scope | T24 |
| Added: CI completion loop | T25 |
| Added: tasks/threads/plans fragmentation | T22 |
| Added: index loads all facts / token growth | T20 |
| Added: repeated quality evaluation | T29 |

## 6. Validation commands and rollout acceptance

Use the repository's gates for release, not a manually chained substitute. Run
focused checks during each task and record exact results. Prefix shell commands
with `rtk` per local instructions (use `rtk proxy` for unsupported commands).

```bash
uv run pytest tests/test_previews.py tests/test_sandbox_processes.py -q
uv run pytest tests/test_deploy_host_script.py -q
uv run pytest tests/test_agent_run_queue.py tests/test_agent_resume.py -q
uv run pytest tests/test_chat_memory.py tests/test_compiler.py -q
cd frontend
npm run build
npm run lint
npm test -- --run
npm run test:integration
npm run test:e2e
```

Release gate from repository root: `scripts/gates.sh`. Migration gate:
`scripts/gates.sh --with-reset` only on a confirmed disposable database, followed
by required role restoration as implemented in that gate. Model/scenario gate:
`scripts/gates.sh --with-agent-eval` when external scope and fixtures are ready.
Real GitHub checks: `uv run pytest -m realgithub` only for the authorized test repo.

### Core public-rollout acceptance

- [ ] Preview JavaScript cannot access app sessions; preview containers cannot reach
      other teams, metadata services, or platform-private endpoints.
- [ ] Linux packaged deployment can clone, install, check, preview, stop, and clean up
      as configured; failed build/migration leaves previous release serving.
- [ ] Two teams progress concurrently, one thread stays ordered, stale workers cannot
      commit effects, and maintenance runs under sustained traffic.
- [ ] Latest and older messages remain accessible; refresh/reconnect/retry/cancel
      preserve one logical run and truthful status.
- [ ] Restricted metadata/history/files/previews remain restricted through invite,
      removal, export, reconnect, and restore.
- [ ] Knowledge has valid provenance, request/decision distinctions, bounded capture,
      correction/deletion handling, and durable thread constraints.
- [ ] Verification corresponds to the proposed patch; CI results return to its thread.
- [ ] Per-team/host budgets, alerts, structured redacted logs, restore and rollback
      drills, and measured quality/capacity evidence exist.
- [ ] No unresolved P0/P1 security or correctness issue is hidden by a feature flag,
      skipped test, absent integration environment, or optimistic documentation.

Optional Phase G ships only after its own isolation and revocation gates. Completing
this plan must never be inferred merely from creating the named files or migrations.
