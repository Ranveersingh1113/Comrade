# Production readiness fixes and repair reviews

Review baseline: `claude/t01-baseline` at `0699aa1` (T19).
Created: 2026-09-07.

This is a repair checklist for completed T01–T19 work, not a replacement for the
master plan. The original sections cover T01–T19; the appended review covers
T20–T27 at `7be319e`. The 2026-09-09 repair review additionally covers committed
T28 backup tooling through `c8f03c6`; later work is excluded. Code locations refer to the reviewed
commit; use function names if subsequent edits move lines. Recheck each finding
against the current branch before editing so fixes already made are preserved.

Sources:

- `docs/superpowers/plans/2026-09-06-production-readiness-master-plan.md`
- `docs/superpowers/plans/2026-09-06-production-readiness-progress.md`
- Read-only review of the implementation through `0699aa1`.

Evidence limits: these findings were established by code tracing. Isolated
execution also confirmed the invalid-decision fallback and missing-source result.
The review did not run shared database tests, production deployment, real browser
isolation tests, or container integration tests. Do not describe those checks as
passing until they have actually run.

## Execution and completion rules

1. Repair deployment and isolation first: F01–F04.
2. Repair dependency execution and resource reclamation: F05–F08.
3. Repair memory integrity: F12–F16, then A01–A02.
4. Repair interaction and scheduling: F09–F11.
5. Finish evidence gates A03–A06 and run the repository gates.

Reuse existing helpers and test infrastructure. Fix shared callers rather than
adding route-specific exceptions. A migration changing a policy must include the
matching grants. Never put identity or permission authority in model arguments.

For each item, record the fix commit, regression command, actual result, and any
remaining limitation in the existing progress ledger. A mock/argv test alone does
not close an item whose failure depends on a browser, Docker, or concurrency.
Keep affected tasks marked partial until their required checks pass.

Use a disposable database/stack for destructive and container integration checks.
Do not run competing suites against the database Claude's active work uses. Follow
the repository's worker-stop requirement before database gates. Do not roll back
or overwrite unrelated T20 changes.

## Confirmed implementation defects

### F01 — Make production proxy configuration valid with previews disabled

- [ ] **Priority:** P1. **Tasks:** T02, T05.
- **Locations:** `docker/Caddyfile:53–55`; `docker-compose.prod.yml:12–16`;
  deployment readiness checks in `scripts/deploy_host.sh`.
- **Failure:** the wildcard preview site and `dns` directive are unconditional.
  Production Compose supplies only `COMRADE_HOST`, so preview/provider arguments
  are empty inside Caddy. The stock image also has no DNS provider plugin. Caddy
  can reject the configuration and take the public site down while a direct API
  readiness request still passes.
- **Required fix:** include the preview site only when its configuration is
  enabled and complete. Supply the selected DNS module and runtime configuration
  in that enabled deployment. Do not use unrestricted on-demand certificates.
  Validate the rendered configuration using the actual image before activation;
  include a check through the public proxy after activation.
- **Acceptance:** preview-disabled configuration validates and serves the app;
  preview-enabled configuration validates with its actual provider module;
  incomplete enabled configuration fails before replacing the healthy stack;
  a stopped/broken Caddy causes the release smoke check to fail.

### F02 — Prevent sandbox traffic back into the platform API

- [ ] **Priority:** P1. **Tasks:** T03, T05.
- **Locations:** `agent/processes.py:130` (`_create_network`/network setup);
  `COMRADE_PREVIEW_PROXY_CONTAINER` wiring in `docker-compose.yml`.
- **Failure:** every preview network includes the real API container. An internal
  Docker network still permits traffic between its members, so repository code
  can connect to the API's port 8000. The API's normal authentication does not
  satisfy the required network boundary.
- **Required fix:** separate preview transport from privileged application
  endpoints, or enforce equivalent directional traffic filtering. The sandbox
  must not reach platform services through the proxy endpoint. Keep Docker socket
  access and platform credentials out of an untrusted or unnecessarily privileged
  transport container. Preserve authenticated proxy-to-preview access.
- **Acceptance:** on a real Linux daemon, run two preview containers. From each,
  attempts to reach the other preview, API/database service ports, host services,
  metadata endpoints, and external addresses fail. Authorized browser preview
  traffic still works. Include reverse traffic toward the proxy in the test.

### F03 — Bound WebSocket messages from untrusted preview servers

- [ ] **Priority:** P1. **Tasks:** T04, T07.
- **Location:** `server/app.py:1298`, `websockets.connect(..., max_size=None)`.
- **Failure:** the upstream iterator assembles complete messages without a size
  limit. An untrusted preview server can exhaust API memory despite HTTP limits.
- **Required fix:** set a finite message limit appropriate to supported preview
  traffic. Check both forwarding directions for equivalent unbounded assembly.
  Handle oversized-message closure and release both connections without a stuck
  forwarding task. Preserve normal HMR behavior.
- **Acceptance:** an oversized upstream message, including fragmented delivery,
  closes the connection within the configured bound; API memory stays bounded;
  subsequent requests succeed; normal HMR messages still pass.

### F04 — Revoke access to already-open run streams

- [ ] **Priority:** P1. **Tasks:** T10, T13, T14.
- **Locations:** `server/app.py:336` (`_run_frames`), `:414–427`
  (`agent_run_stream`), and the compatibility POST stream.
- **Failure:** membership and thread visibility are checked only at connection
  time. Subsequent privileged reads have no viewer identity or access recheck.
  Removing a participant does not stop new private output reaching that stream.
- **Required fix:** bind the authenticated viewer into the shared stream path and
  revalidate current membership/thread visibility when fetching subsequent
  output. Close on revocation without leaking the next batch. Apply this to both
  stream entry points; preserve cursor replay for still-authorized viewers.
- **Acceptance:** open streams as a restricted-thread participant and another
  authorized member; remove the first participant, append new private steps,
  and verify only the remaining authorized viewer receives them. Repeat for team
  membership removal. Reconnection by the removed viewer is denied.

### F05 — Bound dependency-install output while it is produced

- [ ] **Priority:** P1. **Tasks:** T07, T08.
- **Location:** `agent/sandbox.py:659–661`, dependency setup subprocess.
- **Failure:** `subprocess.run(capture_output=True)` buffers arbitrary install-hook
  output for up to the setup timeout. Truncation after completion cannot protect
  pipeline-worker memory.
- **Required fix:** reuse the bounded subprocess/output runner used by normal
  contained commands. Preserve exit status, timeout, truncation status, container
  termination, and network cleanup. A full output buffer must not deadlock pipes.
- **Acceptance:** an actual setup hook emits more than the cap on both stdout and
  stderr. Retained output and worker memory stay bounded; timeout or nonzero exit
  remains visible; setup resources are reclaimed.

### F06 — Make advertised dependency recipes install into usable volumes

- [ ] **Priority:** P1 for Node support; P2 for uv path failure. **Task:** T08.
- **Locations:** `pipeline/repo_deps.py:108–123`; setup mounts/environment in
  `agent/sandbox.py`; `docker/sandbox.Dockerfile:23–30`.
- **Failure:** npm/pnpm target `/deps`, while package manifests and lockfiles are
  in `/workspace`. The default sandbox lacks Node/npm/pnpm. `uv sync --project
  /workspace` targets `/workspace/.venv`, but setup mounts that checkout read-only.
- **Required fix:** provision the runtimes the product advertises. Use an
  explicit writable dependency layout containing the manifests/lockfiles the
  chosen installer actually reads. Point uv at the dependency-volume virtualenv,
  handling root-project installation without writes to the checkout. Ensure
  execution and preview processes use that same environment. Verify sibling
  recipes, including Poetry, against the same read-only mount contract.
- **Acceptance:** build the actual sandbox image and install small npm, pnpm,
  and uv locked projects through the real setup path. Each then runs/imports its
  installed dependency through `repo_run`; a preview uses the same dependencies.
  Changed lockfile changes the environment fingerprint; inconsistent lockfiles
  fail visibly. No checkout writes or unauthorized direct network access occur.

### F07 — Recover containers launched before their ID was persisted

- [ ] **Priority:** P1. **Task:** T06.
- **Locations:** `agent/processes.py:494–495` and stop/reap paths;
  `supabase/migrations/20260907100000_process_lifecycle.sql:31`.
- **Failure:** a crash after `docker run` but before recording `container_id`
  leaves a live container and a row with a persisted name but null ID.
  Reconciliation and deletion cleanup exclude that state.
- **Required fix:** resolve ownership and inspect/stop by the server-generated
  persisted name when the ID is absent. Cover restart reconciliation, explicit
  stop, expiry, and thread/team deletion. Distinguish confirmed absence from a
  daemon failure; never remove an unrelated container on a name mismatch.
- **Acceptance:** inject failure immediately after successful launch and before
  the DB update. After restart, the row is reconciled truthfully and stop/deletion
  reclaims the container and network. A daemon outage remains retryable rather
  than falsely marking cleanup complete.

### F08 — Make deleted-thread cleanup complete and safely retryable

- [ ] **Priority:** P2. **Task:** T06.
- **Location:** `agent/processes.py:536–552`, pending cleanup drain.
- **Failure:** network removal runs while its proxy endpoint is attached. Docker
  rejects it. A retry can then fail removing the already-removed container; the
  oldest failed rows consume the fixed 50-row batch and starve later cleanup.
- **Required fix:** reuse the normal proxy-disconnect/network-removal path.
  Treat verified already-absent resources as successful cleanup. Preserve real
  errors and make progress past retrying rows, without an unbounded hot loop.
- **Acceptance:** delete a thread with a live preview; no container/network remains
  and cleanup is acknowledged. Inject failure after container removal, retry,
  and verify completion. With 50 retrying rows plus a healthy cleanup row, the
  healthy row is eventually processed. Do not suppress daemon failures.

### F09 — Release the composer after message acceptance

- [ ] **Priority:** P2. **Tasks:** T10, T11, T15.
- **Location:** `frontend/src/screens/GroupRoom.tsx:329–369`, `send`/`deliver`.
- **Failure:** `sending` stays true until `follow()` finishes the whole run.
  The originating user cannot send corrections or steering during execution.
- **Required fix:** scope the send guard to admission/persistence, not stream
  lifetime. Follow the accepted run independently. Preserve protection against
  duplicate clicks and uncertain-request retries. Steering must continue to the
  same durable run according to the backend's existing contract.
- **Acceptance:** hold a run open in a component/browser test, submit a correction
  from the same tab, and verify it is accepted and delivered as steering. Rapid
  double-click still creates one initial request. No reload is needed to enable
  the composer, and sending a team message also remains possible where supported.

### F10 — Retry transient stream transport failures

- [ ] **Priority:** P2. **Tasks:** T10, T13.
- **Locations:** `frontend/src/screens/GroupRoom.tsx:171–188`; `followRun` in
  `frontend/src/lib/agentApi.ts`.
- **Failure:** clean incomplete streams retry, but a thrown fetch/body-reader
  error breaks the loop immediately. Common connection drops do not recover.
- **Required fix:** distinguish transient transport failures from aborts and
  permanent authorization/not-found errors. Retry transient failures with a
  bounded delay and the latest received sequence. Do not retry navigation aborts
  or revoked access. Report exhaustion without claiming the run stopped.
- **Acceptance:** force a reader failure after receiving steps, reconnect, and
  receive remaining steps without duplicates or another run submission. Cover a
  rejected initial fetch, user navigation, revoked access, and retry exhaustion.

### F11 — Enforce the team running limit under concurrent claims

- [ ] **Priority:** P2. **Task:** T12.
- **Location:** `supabase/migrations/20260907130000_worker_concurrency.sql:27–55`,
  `claim_next_agent_run`.
- **Failure:** advisory locking covers threads only. Two workers claiming
  different threads can observe the same team count and both admit work beyond
  the ceiling. Moving the count into SQL alone does not serialize admission.
- **Required fix:** serialize team admission and perform the count against a
  snapshot that includes earlier admitted work. Preserve per-thread exclusivity,
  cross-team progress, and bounded claims. Merely adding a lock expression to a
  query with a stale snapshot is insufficient. Check both old/new function
  signatures and their callers so no supported claim path bypasses the limit.
- **Acceptance:** synchronized independent DB connections claim different queued
  threads for one team with a single slot left. Exactly one is admitted. Repeated
  contention never exceeds the cap; another team still progresses; two claims
  for one thread cannot run together. Test database behavior, not SQL text.

### F12 — Remove invalid-decision-to-add fallback in the real compiler path

- [ ] **Priority:** P2. **Tasks:** T16, T18.
- **Locations:** `pipeline/compiler.py:383–389` (`validate_decisions`), `:438–440`
  (`consolidate`), and downstream rejection around `:591–602`.
- **Failure:** missing decisions, invalid actions, or unknown targets become
  `action='add'` before the new rejection runs. An unreadable consolidation
  response can become empty decisions and then additions. Malformed output still
  publishes duplicate or contradictory facts and can advance capture.
- **Required fix:** fail/retry or explicitly reject invalid consolidation;
  never synthesize publication from invalid output. Define how partial valid
  results are handled so retry/cursor behavior cannot silently discard failed
  candidates. Apply validation through all compiler callers.
- **Acceptance:** exercise extraction/consolidation/validation/apply together
  with malformed JSON, missing decisions, an invalid action, and an unknown
  revision target. None publishes an active fact through fallback. Retryable
  failure leaves capture recoverable; explicit rejection has a visible record.

### F13 — Reject missing or invalid chat evidence

- [ ] **Priority:** P2. **Task:** T18.
- **Locations:** `pipeline/chat.py:338–342`; `pipeline/compiler.py:504–505` and
  `:611–612` (`excerpt_is_supported` and its caller).
- **Failure:** an invalid/missing source index becomes `None`; verification then
  returns unknown support; quarantine catches only `False`. A fabricated excerpt
  with no valid citation can become active memory.
- **Required fix:** treat absent or invalid source identity as unsupported.
  Resolve source scope server-side. Keep uncertainty about supported source kinds
  distinct from a missing source; neither may silently become verified evidence.
  Coordinate document/GitHub handling with A01.
- **Acceptance:** through the real chat compile path, missing/out-of-range source
  indices, missing messages, wrong-team sources, and fabricated excerpts never
  become active facts. A real quote from an authorized source still publishes
  with the correct citation and appropriately limited trust.

### F14 — Bind every revision to the version consolidation actually read

- [ ] **Priority:** P2. **Task:** T18.
- **Locations:** `pipeline/github.py:485–490`; `pipeline/compiler.py:649–652`.
- **Failure:** GitHub consolidation does not call `bind_to_seen_versions`.
  Apply treats a missing expected version as a wildcard, allowing a stale
  GitHub result to overwrite a newer chat/document revision.
- **Required fix:** bind expected versions in a common path or every applicable
  caller, and reject/retry a revision without the required version. On conflict,
  consolidate against fresh state or preserve a visible conflict; do not silently
  overwrite or turn the revision into an addition.
- **Acceptance:** pause GitHub consolidation after its read, commit a competing
  revision, then resume it. The newer version survives and conflict/retry is
  visible. Cover chat/document callers and duplicate application as well.

### F15 — Use the compound capture cursor in the sweep as well as fetch

- [ ] **Priority:** P2. **Task:** T16.
- **Location:** `pipeline/chat.py:300–304`, sweep candidate query.
- **Failure:** the sweep uses timestamp-only `>` although bounded fetching uses
  `(created_at, id)`. Messages tied at a batch boundary may never trigger another
  job unless later chatter arrives.
- **Required fix:** use the same timestamp/ID ordering and boundary semantics
  in candidate discovery, bounded fetch, cursor persistence, and retry handling.
- **Acceptance:** insert 61 messages with exactly the same timestamp and stable
  ordered IDs. Run normal sweep/worker cycles without adding newer messages;
  every row is eventually considered exactly as intended. Repeat with a byte-cap
  boundary inside a tied group and with a failed batch followed by retry.

### F16 — Keep the unsummarized conversation visible between compactions

- [ ] **Priority:** P2. **Task:** T19.
- **Locations:** `agent/runtime.py:255–269`; `pipeline/compaction.py:75–79`;
  history/cursor helpers in `agent/history.py`.
- **Failure:** the summary covers an old prefix, but runtime replays only the
  latest 20 messages. Compaction waits for another 40 older messages, so up to
  39 messages fall between the summary and replay window.
- **Required fix:** build context from the summary plus the complete unsummarized
  tail, retaining the required recent-message window without duplicate replay.
  If token limits require further compaction, compact the missing range safely;
  do not silently drop it. Preserve stable compound-cursor ordering.
- **Acceptance:** with a summary through message 40, message 61 must still see a
  constraint in message 41. Test every boundary through the next compaction,
  equal timestamps, and restart. There must be no context gap or duplication of
  messages already represented by the selected summary/replay contract.

## Unfinished acceptance work and additional verified protocol defect

These items are not all newly discovered regressions. Several were acknowledged
as limitations in the progress ledger, but remain requirements of T01–T19.

### A01 — Finish evidence validation for document and GitHub sources

- [ ] **Task:** T18. **Location:** `pipeline/compiler.py:507–511` and document/
  GitHub extraction callers.
- The current verifier returns unknown for non-message sources and allows them
  through. Preserve the exact source text/revision needed for verification, or
  pass a server-bound source snapshot into validation. Resolve it under the
  correct scope; do not accept a model-supplied quote as its own evidence.
- Unsupported candidates must remain proposed/quarantined. Expose source, date,
  trust, and supersession in the wiki as required by T18. Ensure the correction/
  revert path remains usable and audited. Do not label substring agreement as
  proof of semantic truth. Do not invent an automatic confirmation workflow.
- **Acceptance:** real and fabricated excerpts from each supported source kind;
  source revision changes; wrong scope; member correction/revert; visible trust
  state. Missing verification must not default to active publication.

### A02 — Complete the durable working-memory contract

- [ ] **Task:** T19. **Locations:** `agent/history.py`, `pipeline/compaction.py`,
  thread-working-state migration, and thread UI.
- The ledger acknowledges no user inspection/correction UI, no concurrent
  compaction proof, and unpopulated open-question/plan-version/pending fields.
- Implement inspection/correction of pins and summaries under thread RLS. Ensure
  compaction cannot overwrite a correction or a newer summary: bind each write
  to the state/range it read and retry/reject stale writes. Make cursor and summary
  updates atomic. Populate the agreed working-state contract from authoritative
  records where possible; avoid a second drifting copy of approvals or plans.
- **Acceptance:** two overlapping compactions cannot regress the cursor or lose
  newer state; a user correction during compaction survives; pinned constraints
  and unresolved questions survive 100+ messages and restart; unauthorized users
  cannot inspect/edit private state; private state never enters the team wiki.
  Exact approved arguments and permission ownership remain structured authority.

### A03 — Prove preview origin and browser behavior

- [ ] **Tasks:** T02, T04.
- Run a real-browser sentinel test using harmless app localStorage data. A
  preview must not read it. Verify sibling-host isolation, host-only cookies,
  grant replay/expiry, token audience, and access removal.
- Run a real small app exercising JS/CSS, absolute/relative URLs, navigation,
  form/API requests, redirects, encoded/repeated query keys, and HMR.
- Use a controlled TLS setup or disposable environment. Lack of local DNS/TLS
  is a recorded missing proof, not a passing result or permission to reopen an
  unsafe same-origin preview.

### A04 — Reject response overflow without a successful truncated load

- [ ] **Priority:** P2. **Task:** T04.
- **Location:** `server/app.py:1218–1223`, HTTP preview streaming generator.
- **Failure:** returning normally when a chunked response exceeds the cap ends
  the response cleanly with the upstream status, commonly 200. The browser sees
  a successfully completed but truncated resource.
- **Required fix:** reject known oversized content before sending headers. For
  overflow discovered after headers, abort the stream in a way the client
  observes as failure and close the upstream. Do not try to replace a status
  after headers have already been sent.
- **Acceptance:** test known-length and chunked oversized responses over an
  actual HTTP connection. Neither completes as a successful truncated resource;
  retained memory is bounded and a later normal request succeeds.

### A05 — Prove packaged execution and deployment failure handling

- [ ] **Tasks:** T01, T05, T06, T08.
- Build the actual execution image and run through disposable Linux Compose:
  `repo_run`, dependency setup, preview startup, stop, and restart reconciliation.
  Check real non-root bind ownership, socket GID, CLI availability, and sandbox
  image availability. Host-installed tools must not mask image omissions.
- Exercise build failure, migration failure, host-lock contention, and failed
  readiness/public-proxy smoke checks. A failed build or migration must leave
  the healthy stack intact; record the previous immutable image for rollback.
- Unit shell/argv assertions supplement this check; they do not replace it.

### A06 — Close the gates with evidence rather than counts alone

- [ ] **Tasks:** T01–T19 validation.
- Run focused regressions for repaired paths, then the repository's canonical
  `scripts/gates.sh`; run `scripts/gates.sh --with-reset` against the designated
  disposable database where required. Restore local roles after any reset as
  documented by the repository. Keep queue-draining workers stopped for tests.
- Include the real browser, Docker network, packaged execution, and simultaneous
  DB-claim checks described above. Keep unavailable/failed lanes explicit.
- Update the existing progress ledger with exact commands, environment, commit,
  results, and remaining acceptance gaps. A task is complete only when both its
  implemented behavior and its required checks are satisfied.

## Follow-up: intended ASCII Box backend is not connected

### F17 — Connect ASCII Box to the real execution and preview paths

- [ ] **Priority:** P1 for the intended Box deployment. **Related tasks:** T03–T08.
- **Added:** 2026-09-08, following the user's clarification that ASCII Box is the
  intended sandbox service. This is a follow-up to the pinned T19 review, not a
  claim that the original T19 implementation promised an active Box backend.
- **Verified locally:** `box.exe` is installed at
  `C:\Users\ricky\.ascii\bin\box.exe`. `agent/box.py` contains an ASCII Box API
  client, but current production execution callers do not use `BoxClient`.
  `shared/config.py:191` defaults to Docker; `agent/sandbox.py:433` and
  `agent/processes.py:243` explicitly reject non-Docker execution and previews.
  Installation on the developer's machine does not prove installation or active
  configuration on EC2. The deployed backend remains unverified.
- **Required fix:** wire the existing Box client into the configured backend's
  actual command, dependency setup, and preview lifecycle paths. Prefer the
  existing server-side API client; do not add CLI subprocess plumbing merely
  because the CLI is installed. Verify provider capabilities before mapping
  Docker assumptions to Box. Do not simply remove the rejection guards.
- Bind team/thread identity server-side. Keep provider credentials outside
  repository environments. Define workspace upload/download and revision
  ownership so edits, execution, previews, and later PR creation use the same
  intended files. Preserve output/resource bounds, restricted setup egress,
  cancellation, expiry, cleanup, and restart recovery. An ambiguous command
  submission must not be automatically retried and executed twice.
- Preserve authenticated preview access and browser-origin isolation. Prove
  Box's network/credential boundary satisfies the existing contracts before
  enabling it; record unsupported capabilities and fail closed. Never silently
  fall back to Docker when Box is selected.
- Configure the intended backend explicitly in deployment, validate required
  configuration at startup, and expose the selected provider in operator-facing
  health/diagnostics without exposing secrets. Confirm the deployed provider
  separately from local CLI availability.
- Reassess Docker-specific fixes F02/F05–F08 and packaged checks A05 after the
  provider decision: retain them for supported Docker paths, or explicitly mark
  them superseded by tested Box equivalents if those paths are retired. Do not
  build two production execution systems unnecessarily. Remove socket mounts
  and host-only dependencies only after all remaining Docker callers are gone.
- **Acceptance:** in a disposable Box, run the real Comrade tool path to upload
  a small repository, install a locked dependency, execute a command, preserve
  resulting workspace changes, start an authenticated preview, cancel/stop, and
  clean up. Verify isolation, output limits, no inherited platform secrets,
  expired/revoked preview access, worker restart, and ambiguous submission
  handling. Selecting Box must produce provider-side evidence of Box execution
  and no local Docker launch. An unavailable/misconfigured Box backend must fail
  visibly without fallback. Record the tested provider configuration and results
  in the progress ledger.

## Scope guard

This checklist does not assert that unlisted T01–T19 behavior is defect-free.
It records the actionable defects and acceptance gaps found in the pinned review,
plus the explicitly labeled Box follow-up requested on 2026-09-08.
It does not authorize changes to live production, redesign T28+, or mark future
tasks complete. Implement the smallest shared fix that satisfies each contract.

## Review extension through T27 — 2026-09-08

**Pinned revision:** `7be319e`, including completed T26/T27 work. Reviewed the
T20–T27 delta from `0699aa1` and traced affected callers, policies, and triggers.
T28's uncommitted backup/operations work was excluded. No application code was
changed. No shared database, live AWS, real-GitHub, or container mutation tests
were run. Evidence below distinguishes deterministic code defects from openly
acknowledged unfinished requirements.

**Act on this now.** Finish the current coherent change, then prioritize private
data access (F18–F21), verification (F26–F27), budget enforcement (F30), and log
privacy (F31–F32), alongside the earlier release blockers. Repair the broken CI
integration before building further behavior on its records. Do not wait for
T28/T29 to turn these into a larger integration repair.

**Earlier findings:** spot checks found `docker/Caddyfile`, `GroupRoom.tsx`,
`agent/box.py`, `agent/sandbox.py`, `pipeline/chat.py`, and `agent/history.py`
unchanged between the two reviewed commits. Those earlier findings have not
been closed by later task completion. This is not a claim that every earlier
finding received a fresh full-system reproduction; keep their checkboxes open
until their specific acceptance evidence exists.

### F18 — Apply private attachment visibility to Storage itself

- [ ] **Priority:** P1. **Task:** T24.
- **Locations:** `20260908100000_thread_attachments.sql:47–55` versus
  `supabase/migrations/20260721090000_storage_documents.sql:23–27`.
- **Failure:** metadata is thread-scoped, but Storage SELECT still authorizes
  every team member by the first path segment. A same-team nonparticipant can
  list/read a restricted attachment directly through Storage, bypassing the
  document API and frontend.
- **Required fix:** bind binary objects to canonical document ownership and
  enforce current document/thread access in Storage policies. Define signed URL
  lifetime/revocation behavior explicitly; a previously issued bearer URL may
  remain usable until expiry. Include soft-deleted documents and orphan objects.
- **Acceptance:** actual authenticated Storage list, download, and URL-signing
  requests by a nonparticipant fail; a participant succeeds; removal prevents
  new access. Test signed URL expiry separately. UI-only denial is insufficient.

### F19 — Keep restricted content out of team-wide audit snapshots

- [ ] **Priority:** P1. **Tasks:** T22, T24.
- **Locations:** `supabase/migrations/20260612101500_triggers.sql:83–100,107–115`;
  `20260612095500_rls.sql:212–213`; new task/document visibility policies.
- **Failure:** task/document triggers copy entire rows into `change_log.before`
  and `after`. Its SELECT policy remains team-wide. Restricted task content,
  filenames, object paths, and populated document text remain readable through
  audit history even when direct row access is denied.
- **Required fix:** preserve source visibility in audit storage and reads,
  including after source deletion. Minimize unnecessary content snapshots.
  Address already-written records as well as new writes; do not destroy audit
  evidence or temporarily expose it during migration.
- **Acceptance:** a nonparticipant cannot recover private content from audit
  queries after insert, edit, participant removal, or source deletion. Authorized
  audit access and required correction/revert behavior still work.

### F20 — Validate object ownership before privileged Storage reads

- [ ] **Priority:** P1. **Tasks:** T21, T24, T26.
- **Locations:** `server/app.py:1174–1196`; `pipeline/compiler.py:840–846`;
  `shared/storage.py:42–50`; document INSERT policy in
  `20260612095500_rls.sql:138–139`.
- **Failure:** members can assign `documents.storage_path`, and the worker reads
  that path with the service secret. A member knowing another team's object path,
  or a restricted same-team path, can create a visible document alias and import
  its contents. The old reingest path already had this risk; initial ingestion
  now extends it. Knowledge of the foreign path is a prerequisite, not authority.
- **Required fix:** resolve and validate canonical object/document ownership and
  source access at the privileged read boundary. Prevent reassignment/aliasing
  through direct metadata writes. Checking only the team prefix is insufficient
  for restricted same-team objects. Apply the guard to ingest, retry, and reads.
- **Acceptance:** known foreign-team and restricted same-team paths cannot be
  imported into a caller-visible document. No bytes, parsed text, or wiki facts
  escape. Legitimate object references continue to work.

### F21 — Recheck deletion and purpose atomically with publication

- [ ] **Priority:** P1. **Tasks:** T21, T24.
- **Locations:** `pipeline/compiler.py:738–758`, `:792–799`, `:872–885`,
  `:911–931`.
- **Failure:** purpose is checked only at enqueue. A queued team-knowledge file
  demoted to private context still compiles. Deletion is checked before
  `compile_document`, but that function performs model calls before its separate
  apply transaction. Deletion/demotion during those calls therefore does not
  prevent publication. The final ready/text update also lacks a deletion guard.
- **Required fix:** validate current source state before expensive work and again
  in the transaction applying facts, with concurrency control that excludes a
  conflicting deletion/demotion. Bind to the source revision processed. Do not
  hold a transaction open during model calls. Avoid restoring deleted rows to a
  ready state. Coordinate derived-fact retention with A08.
- **Acceptance:** pause processing before claim and during model work; delete or
  demote the source; resume. Neither case publishes into the team wiki or marks
  the deleted source ready. Race the final apply against deletion as well.

### F22 — Check the recorded source hash before parsing

- [ ] **Priority:** P2. **Task:** T21. **Acknowledged ledger limitation.**
- **Locations:** `pipeline/compiler.py:840–846` and `enqueue_document`;
  `server/app.py:1187–1196`.
- **Failure:** `content_sha256` is recorded but never compared with fetched bytes.
  Replacing content at the same path silently compiles different input.
- **Required fix:** establish a server-validated source digest/version and check
  it on fetch. Reject changed content or explicitly enqueue a new revision.
  Define compatibility for old jobs without a hash; do not imply they were
  verified. Retry requests must bind the intended revision too.
- **Acceptance:** change the object between enqueue and fetch; publication is
  refused or explicitly processes a new acknowledged revision. Unchanged bytes
  succeed, and the recorded citation identifies that source revision.

### F23 — Enforce upload limits on every ingestion path

- [ ] **Priority:** P2. **Tasks:** T21, T26.
- **Locations:** `server/app.py:1192,1210–1217`; inline-content branch in
  `pipeline/compiler.py:836–839`; `shared/storage.py:26`.
- **Failure:** the 25 MiB download cap does not cover either unbounded
  `file.file.read()` in the API. The legacy no-storage route can still enqueue
  and parse oversized inline content. With a stored path, a supplied upload is
  still fully allocated merely to hash it.
- **Required fix:** enforce request/file limits before whole-file allocation and
  incrementally hash permitted uploads. Apply the worker-side cap to legacy
  payloads as well. Bound decoded binary size and consider parser expansion;
  compressed file size alone is not a parser memory guarantee.
- **Acceptance:** oversized uploads with and without a storage reference fail
  with a clear size error and bounded API memory; oversized legacy jobs fail
  before parsing/model work. Permitted uploads still work on both paths.

### F24 — Repair the real CI webhook call and durable acknowledgment

- [ ] **Priority:** P1 for T25 functionality. **Task:** T25.
- **Locations:** `server/app.py:1135–1142`; `pipeline/ci.py:116–119`.
- **Failure:** the route passes delivery ID as a fifth positional argument to a
  keyword-only parameter. Every supported check event raises `TypeError`, gets
  logged, and is acknowledged without its result record. Fixed-SHA signature
  execution reproduced: `takes 4 positional arguments but 5 were given`.
- **Required fix:** pass `delivery_id=` explicitly. Persist a recoverable delivery
  before success acknowledgment; correlation failure must have a durable retry
  path or an appropriate failed response, not only a log. Avoid losing early
  events whose PR mapping arrives later.
- **Acceptance:** a signed delivery through the real route creates the result;
  redelivery does not duplicate it. Inject persistence failure and prove later
  recovery. Helper-only tests do not cover the broken call site.

### F25 — Return actual PR identity to the correlation caller

- [ ] **Priority:** P1 for T25 functionality. **Tasks:** T23, T25.
- **Locations:** `shared/consent.py:622–631`; `pipeline/repo_pr.py:177,191`.
- **Failure:** consent execution reads `result['number']`, but real PR creation
  returns only `pr_url` and `created`, including its already-existing PR path.
  The caught `KeyError` leaves no originating-thread record. `head_sha` is also
  absent, preventing trustworthy stale-head classification after only fixing
  the number.
- **Required fix:** return the actual GitHub PR number and head SHA on both paths
  and persist correlation reliably. If the external PR opens but the local write
  fails, recover the mapping without opening/pushing a duplicate PR.
- **Acceptance:** test consent execution through the real `_create_pr` adapter
  with mocked HTTP transport, not a fabricated helper return. Verify number,
  branch, head, thread, and action correlation for creation and existing PRs;
  test recovery after the external success/local-write failure boundary.

### F26 — Include new-file contents in verification identity

- [ ] **Priority:** P1. **Task:** T23.
- **Locations:** `agent/repo_tools.py:428–440`, `_unverified` around `:484`.
- **Failure:** the digest hashes `git diff HEAD` plus untracked filenames, not
  untracked contents. Create a new file, pass a check, rewrite that file under
  the same name, then propose: the digest is unchanged and the gate passes.
  Patch capture subsequently includes unchecked contents.
- **Required fix:** digest the exact candidate content/patch, including untracked
  contents and relevant metadata. Ensure verification and proposal capture
  identify the same revision; account for background mutations between them.
- **Acceptance:** use a real temporary Git repository. Change a previously
  verified new file without renaming it; proposal must fail until reverified.
  Cover binary/new files and changes between digest measurement and capture.

### F27 — Fail closed when verification identity cannot be measured

- [ ] **Priority:** P2. **Task:** T23.
- **Location:** `agent/repo_tools.py:428–440`.
- **Failure:** git exit codes are ignored. Repeated failures with empty stdout
  produce the same valid-looking digest. The exception fallback
  `unreadable:{id(root)}` is also stable for the same root object. Both behaviors
  were reproduced by isolated execution of the committed function.
- **Required fix:** represent failed measurement as an error, never an identity.
  Refuse verification recording and proposal when measurement fails. Check exit
  status, timeout, and missing repository behavior explicitly.
- **Acceptance:** nonzero git exit, timeout, and OSError cannot create or match an
  accepted verification record, even when repeated with the same root object.

### F28 — Synchronize linked state after reassignment resets a task

- [ ] **Priority:** P2. **Task:** T22.
- **Locations:** `20260908090000_task_thread_link.sql:79–81`;
  `20260612101500_triggers.sql:17–20` and task-update executor.
- **Failure:** reassignment updates `assignee_id`; the BEFORE confirmation guard
  changes status to proposed. The thread-state trigger listens only to explicit
  updates of status/thread_id, so it does not run. A done thread remains done
  while its task becomes proposed.
- **Required fix:** trigger synchronization for all relevant changes, including
  status changes made by BEFORE triggers. Use change detection to avoid needless
  writes. Preserve the task/thread state contract.
- **Acceptance:** complete a linked task, then reassign through the actual update
  path; task becomes proposed and thread becomes planned in the same transaction.

### F29 — Enforce thread access on linked-task INSERT

- [ ] **Priority:** P2 authorization defect. **Task:** T22.
- **Locations:** `20260908090000_task_thread_link.sql:56–73`;
  `20260612095500_rls.sql:171–172`.
- **Failure:** task SELECT/UPDATE were narrowed but INSERT still checks only team
  membership. A nonparticipant knowing a restricted work-thread ID can insert a
  linked task; the security-definer trigger changes that thread's work state.
- **Required fix:** require access to the linked thread on INSERT, alongside
  team/type integrity. Audit other link-creation paths against the same rule.
- **Acceptance:** a same-team nonparticipant cannot insert a task referencing a
  known restricted thread, and its state remains unchanged. Authorized creation
  succeeds. Exercise direct authenticated database/API access.

### F30 — Allocate shared budget headroom atomically

- [ ] **Priority:** P1 for spend enforcement. **Task:** T26.
- **Location:** `shared/usage.py:175–188`; runtime budget brake.
- **Failure:** each active run receives its reservation plus the same unclaimed
  team headroom. Active spend reaches the bucket only at finalization. Isolated
  execution with a 500,000 cap and two 6,000 reservations gives each run a 494,000
  allowance; each can spend 490,000 while their combined spend is 980,000.
  This concurrency defect is also acknowledged in the progress ledger.
- **Required fix:** atomically allocate incremental call budgets and reconcile
  actual spend idempotently. Bound individual calls as far as the provider
  permits. Cover retries, waiting/resume, lease loss, cancellation, hour changes,
  and missing usage metadata without undercounting or charging twice.
- **Acceptance:** synchronized runs cannot both acquire the same remaining budget.
  Explicitly measure any unavoidable in-flight overshoot. Repeated finalization
  and retry cannot refund or spend the same allocation twice. See A11 for crash
  linkage and non-agent quotas.

### F31 — Apply log redaction to actual Uvicorn handlers

- [ ] **Priority:** P1 privacy coverage. **Task:** T27.
- **Location:** `shared/observability.py:143–154`; actual Uvicorn logging setup.
- **Failure:** setup replaces only root handlers. Uvicorn's default access logger
  has its own handler and `propagate=False`, so its records never pass through
  the root redactor. This was verified locally using installed Uvicorn's real
  `LOGGING_CONFIG`: root had `RedactingFilter`; access had no filters and did not
  propagate. Request paths/query values can therefore leave through access logs.
- **Required fix:** configure redaction/minimized request logging at every actual
  output handler, including Uvicorn access/error logging. Do not rely on root
  setup to govern non-propagating loggers. Preserve useful request correlation.
- **Acceptance:** start the API with its packaged Uvicorn invocation, send a
  harmless query-token sentinel, and inspect captured access/error output. The
  sentinel never appears; status, route, and correlation remain usable.

### F32 — Avoid database input values in the primary error message

- [ ] **Priority:** P2 privacy defect. **Tasks:** T26, T27.
- **Location:** `shared/errors.py:28–68,90`.
- **Failure:** stripping DETAIL/CONTEXT leaves PostgreSQL primary errors such as
  `invalid input syntax for type uuid: 'PRIVATE_DOCUMENT_SENTINEL'` intact.
  Malformed model/source values can consequently enter durable errors and logs
  even without a DETAIL section. The fixed-SHA redactor retained that sentinel
  in isolated execution. This is a concrete primary-message case, beyond the
  ledger's generic warning about unknown secret formats.
- **Required fix:** for database errors, derive safe diagnostics structurally
  from exception type/SQLSTATE and allowlisted diagnostic fields. Do not retain
  arbitrary input-bearing primary messages on the assumption regex catches all
  private content. Preserve intentional safe user-facing application errors.
- **Acceptance:** real invalid UUID/enum/cast errors containing harmless private
  sentinels produce useful error categories without values in queue/run columns,
  document errors, or logs. Existing constraint diagnostics remain actionable.

### F33 — Make readiness detect dead workers before their queues age

- [ ] **Priority:** P1 readiness correctness. **Task:** T27.
- **Locations:** `server/app.py:177–220,365–394` (`ready`, queue/lease checks).
- **Failure:** successful DB credentials plus empty queues make readiness green
  even if both workers are stopped. Agent running leases are absent from the
  expired-lease query, which checks only pipeline jobs. Sandbox availability is
  also explicitly unchecked. Thus deploy readiness still cannot establish that
  a repository turn can run.
- **Required fix:** publish worker heartbeat/capability status independently of
  work arrival, and check freshness and applicable expired leases. Probe sandbox
  capability from the execution worker, not by granting the API a Docker socket.
  Report the selected provider and unsupported/unavailable capabilities honestly.
- **Acceptance:** empty queue with stopped agent/pipeline worker is unready;
  expired running agent lease is reported; unavailable configured sandbox is
  detected; healthy idle workers are ready. Test both Docker/Box only where each
  is actually supported. Do not equate queue age with worker liveness.

### F34 — Measure compiler lag only over eligible uncaptured messages

- [ ] **Priority:** P2 operational correctness. **Task:** T27.
- **Locations:** `server/app.py` metrics lag query around `:286–295`;
  eligibility in `pipeline/chat.py:297–304`.
- **Failure:** metrics compares all messages to the team's compilation cursor.
  It includes private-thread, AI, and deleted messages that capture deliberately
  excludes. A team with only private conversation can report steadily increasing
  compiler lag while there is no eligible backlog at all.
- **Required fix:** share capture eligibility and completed compound-cursor
  semantics with the metric. Measure oldest eligible unprocessed input, not
  every message newer than a timestamp. Coordinate the cursor correction F15.
- **Acceptance:** private-only, AI-only, and deleted-only activity reports no
  compiler backlog. A real uncaptured public user message reports its age;
  unrelated teams' successful compilations cannot hide it.

## Remaining T20–T27 acceptance requirements

These are unfinished original requirements, not additional claims of reproduced
regressions. The progress ledger already admits many of them. Green unit counts
do not turn backend foundations into complete user journeys.

### A07 — Validate retrieval at corpus scale

- [ ] **Task:** T20. Add the planned growing-corpus latency/recall measurements,
  including paraphrase, negation, identifiers, and revision recall beyond the
  consolidation cap. Record prompt/tool/history/memory/file/output token
  attribution. Demonstrate the intended bounded retrieval strategy with evidence;
  do not mark relevance/quality requirements done from query-shape tests alone.

### A08 — Define and enforce source deletion/retention behavior

- [ ] **Task:** T21. Implement the agreed retention policy for raw payloads,
  private steps, logs, artifacts, and exports. Define treatment of already-derived
  facts after source deletion, retaining appropriate audit/provenance rather
  than silently leaving broken citations. Collect orphan uploads safely after
  failed metadata insertion. Test deletion while queued/compiling separately
  from deletion of previously compiled knowledge. F21 covers the race itself.

### A09 — Finish task, attachment, and CI user journeys

- [ ] **Tasks:** T22, T24, T25. Build task-to-thread link creation and the planned
  board state; thread attachment upload/progress/cancel/retry/download and explicit
  promotion with a visibility warning; thread-visible CI checks, links, commit,
  conclusion, and bounded redacted/datamarked failure summaries. Bind attachment
  context to its intended message/turn as required, not only a thread column.
  Implement the specified policy-authorized, quota-bound continuation at most
  once per failure revision, with no automatic patch publication. Prove ordinary
  and restricted-thread browser journeys. Database tables alone are partial work.

### A10 — Use declared or reviewed verification requirements

- [ ] **Task:** T23. The current command heuristic rejects some no-ops but still
  accepts irrelevant tests. Require project-declared checks or an explicitly
  reviewed policy; keep unavailable/stale environments distinct from verified
  code. Exercise a real authorized GitHub PR against the exact checked patch,
  including background writes, restart, and concurrent threads. F26/F27 fix
  digest defects but do not establish that a chosen check is relevant.

### A11 — Complete reservation recovery, service isolation, and workload quotas

- [ ] **Task:** T26. Make reservation/enqueue/linkage crash-safe or reconcile
  durable orphan reservations; waiting for the hourly bucket to expire is not
  recovery. Provide only needed credentials to each service; blanking the admin
  URL while sharing the rest of `.env` does not finish that requirement. Add the
  specified compilation/setup/upload/external-work admission limits and useful
  retry/usage feedback. Test crashes at every reservation boundary, simultaneous
  admission, terminal paths, and key rotation. Coordinate with F30's incremental
  shared spend; avoid independent accounting systems that can disagree.

### A12 — Finish operational signals and drain evidence

- [ ] **Task:** T27. Separate process liveness from dependency readiness and keep
  Compose restart documentation accurate. Add the planned request correlation
  and missing tool-error, permission-wait, preview-failure, and denial metrics;
  define how operators retain/scrape the measurements. Wire actionable alerts in
  the deployment rather than treating runbook text as a working alert. Exercise
  drain under continuous load with measured recovery time after forced stop.
  Maintenance currently follows a batch of blocking jobs; prove its required
  cadence under long-running work rather than assuming a batch bound is a time
  bound. Record any remaining unsupported checks explicitly.

**Completion rule for this extension:** attach fix commits and actual regression
results to each item in the progress ledger. Run targeted checks, then the
canonical gates against an exclusively owned disposable stack. Preserve Claude's
uncommitted T28 work. Do not claim production readiness from this static review.

## Repair review — 2026-09-09, pinned to `c8f03c6`

Reviewed committed repairs since `d701f56`, including the T28 backup tooling.
No application edits, shared database tests, live deployments, or Docker
mutations were performed. References below are at `c8f03c6`; subsequent Claude
edits must be checked before applying them. This section supplements earlier
items rather than replacing their acceptance criteria.

**Progress:** the original CI argument/return-shape defects, untracked-file
content hashing, explicit measurement failure, Storage thread visibility,
audit scoping, compiler rejection, and several lifecycle paths now have direct
code fixes and targeted regression tests. This is meaningful progress, not
evidence that every associated acceptance requirement is complete. The reported
1516 backend/224 frontend results are Claude's ledger results, not suites rerun
by this reviewer. Keep F02/F17 and remaining acceptance requirements visible.

**F20 remains incomplete despite the ownership migration.** The unique index
prevents a second document using the exact same path, and the trigger prevents
repointing an existing path. Neither validates ownership of an object with no
document row yet. `shared/storage.py:79–96` still accepts an arbitrary path and
reads with the service secret; it has no team/document argument or ownership
guard. The migration's claim that the reader checks a team prefix is false.
The new Storage SELECT policy also trusts the document's claimed ownership, so
an attacker-created alias to a known unreferenced foreign object can authorize
direct reads as well. Validate canonical object ownership, including the
upload-before-metadata interval; reject noncanonical path aliases. Add tests
with a foreign object that does not already have a document row. Existing
duplicate-path tests do not prove this case. Do not close F20 yet.

### F35 — Probe the configured public hostname during deployment

- [ ] **Priority:** P1 deployment correctness. **Follow-up:** F01/T05.
- **Location:** `scripts/deploy_host.sh:135–144`; `docker/Caddyfile:18`.
- **Failure:** the new proxy check calls `https://localhost/api/health` with
  certificate checking disabled. Caddy's site is selected by `COMRADE_HOST`,
  not localhost; a healthy configured deployment can fail TLS/host routing and
  be reported failed. It also does not establish public endpoint reachability.
- **Required fix:** use the configured hostname with correct Host/SNI and valid
  certificate verification. If checking locally via address override, preserve
  that hostname and distinguish this from an external DNS/routing check. Verify
  API readiness and frontend delivery through the actual configured site.
- **Acceptance:** a healthy non-localhost site passes; wrong hostname, invalid
  certificate, broken proxy routing, and stopped frontend fail visibly. Check
  actual HTTP/TLS behavior rather than only the generated shell command.

### F36 — Treat SQL restore errors as failure

- [ ] **Priority:** P1 recovery correctness. **Task:** T28.
- **Location:** `scripts/backup.py:176–182`.
- **Failure:** database restoration deliberately omits `ON_ERROR_STOP`. psql can
  continue after failed schema/data/policy statements and exit successfully;
  `restore()` then returns elapsed time for a partial restore. The test drill's
  selected assertions do not protect arbitrary callers of this helper.
- **Required fix:** fail on SQL errors. Handle known pre-existing objects through
  an explicit restore strategy/preparation, not by ignoring all statement errors.
  Report incomplete restores and retain diagnostics. Avoid presenting a failed
  target as usable.
- **Acceptance:** inject a failing data or policy statement into a disposable
  restore; the operation reports failure. A valid artifact restores successfully
  with roles, policies, representative data, and authorized/denied reads checked.

### F37 — Never change the requested database server in client fallback

- [ ] **Priority:** P1 wrong-target recovery risk. **Task:** T28.
- **Location:** `scripts/backup.py:101–108`.
- **Failure:** when host PostgreSQL binaries are unavailable, `_run` replaces
  the URL's host/port with `127.0.0.1:5432` inside the local database container.
  A remote backup/restore request silently targets another server. The fixed-SHA
  helper was checked with mocked subprocesses: requested
  `remote.example:6543`, fallback selected `127.0.0.1:5432`.
- **Required fix:** preserve the requested target when using containerized client
  binaries. Permit local-container endpoint translation only through explicit,
  validated local configuration. Fail if the requested target cannot be reached;
  do not fall back to a different database.
- **Acceptance:** missing local binaries never change a remote URL's target.
  Test explicit local-container translation separately, including database name,
  port, connection options, and clear refusal of ambiguous configuration.

### F38 — Publish complete backup sets without overwriting the last good set

- [ ] **Priority:** P2 recovery integrity. **Task:** T28.
- **Location:** `scripts/backup.py:143–161`.
- **Failure:** `globals.sql` is overwritten before the database dump succeeds.
  A later failure leaves new globals beside an older database dump. Reusing the
  output directory also destroys the previous complete set without a successful
  replacement. This contradicts the claimed paired-artifact guarantee.
- **Required fix:** stage each backup in a separate generation, validate both
  artifacts, and publish a completion manifest/pointer only after success.
  Preserve the last successful generation on any failure. Restrict permissions
  on data and role-password artifacts; avoid buffering large dumps unnecessarily.
- **Acceptance:** fail after globals, during database dumping, and before final
  publication. The previous complete backup remains selectable, and no incomplete
  pair is reported complete. Verify successful generations match their manifest.

### F39 — Keep worker heartbeats alive during legitimate long jobs

- [ ] **Priority:** P2 readiness regression. **Follow-up:** F33/T27.
- **Locations:** `agent/worker.py:135–146`; `pipeline/worker.py:433–443`;
  `shared/heartbeat.py:35–36` (`STALE_SECONDS=120`).
- **Failure:** agent heartbeats run before blocking `run_once`; pipeline beats
  run before a whole blocking `tick`. When all agent slots or a pipeline batch
  remain busy for over 120 seconds, healthy workers stop reporting and `/ready`
  declares them missing. The heartbeat is not actually on an independent clock.
- **Required fix:** emit liveness independently of blocking work, while reporting
  progress/capability and draining state separately. Do not make a heartbeat
  imply that a wedged job is progressing. Shut down the heartbeat with the worker.
- **Acceptance:** hold legitimate work beyond the freshness threshold; live
  workers remain visible. Stop the worker and verify expiry. A stuck job is
  diagnosed through lease/progress signals, not hidden by a liveness heartbeat.

### F40 — Do not bypass verification when the current turn's edit count is zero

- [ ] **Priority:** P1 verification integrity. **Follow-up:** F26/F27/T23.
- **Location:** `agent/repo_tools.py:526–533`; runtime session initialization.
- **Failure:** `_unverified` returns False immediately for edit generation zero.
  A resumed/new turn with existing dirty work, or changes produced by a command
  rather than `repo_edit`, can therefore propose without any recorded check or
  digest measurement. Isolated fixed-SHA execution confirmed no verification
  record plus a zero counter bypasses the measurement function entirely.
- **Required fix:** decide from the actual proposed content and durable
  verification evidence, not whether this in-memory turn called the edit tool.
  Let a truly empty patch take the existing empty-patch path. If unverified
  publication is ever supported, require an explicit reviewed policy and label
  it honestly rather than silently bypassing the gate.
- **Acceptance:** dirty workspace after restart and command-only mutation both
  require relevant verification. Zero edit count cannot waive measurement.
  An empty workspace still produces an actionable empty-change response.

### F41 — Make failed PR correlation recoverable after consent completion

- [ ] **Priority:** P2. **Follow-up:** F25/T25.
- **Locations:** `shared/consent.py:633–649`, `:251–279`.
- **Failure:** PR identity now returns correctly, but a correlation DB failure
  is caught and consent commits as executed. The comment says a later attempt
  will repair it; retrying that consent returns `noop` before the executor, so
  normal retry does not perform the missing write. CI can remain unlinked.
- **Required fix:** persist a durable repair intent or reconcile completed PR
  actions lacking a mapping. Retrying bookkeeping must not require another
  member approval or rerun the external publication action.
- **Acceptance:** external PR creation succeeds, correlation fails, consent
  commits, process restarts. A durable retry restores thread/PR/head mapping
  exactly once without opening or pushing another PR.

### F42 — Preserve cumulative usage across permission waits and resumes

- [ ] **Priority:** P1 spend integrity. **Follow-up:** F30/T26.
- **Locations:** `agent/runtime.py:296–297,416–421`;
  `shared/agent_runs.py:174–177`.
- **Failure:** permission wait returns before durable usage accounting; resuming
  the same run resets token totals to zero. Its claimed allocation remains
  cumulative, so subsequent segments reuse spent allowance and finalization can
  charge only the last segment. Atomic chunk claims alone do not close F30.
- **Required fix:** checkpoint cumulative usage before parking and resume from
  it. Apply accounting/braking to every early-return path while keeping permission
  state and accounting distinct. Reconcile each usage event/attempt once.
- **Acceptance:** two permission cycles followed by completion charge all three
  segments, enforce the shared cap across them, and finalize exactly once.

### F43 — Account for in-flight usage without letting stale workers settle a run

- [ ] **Priority:** P1 spend/lease integrity. **Follow-up:** F30/T26.
- **Locations:** `agent/runtime.py:398–409`, `_finish` around `:88–101`.
- **Failure:** cancellation/lease ownership is checked before the arriving
  event's usage is read. Cancellation during the first model call can finalize
  zero tokens and refund the reservation although the response was paid for.
  Lease loss takes the same path and can settle accounting while a replacement
  worker continues the run.
- **Required fix:** durably account for delivered usage before discarding output.
  Fence attempt accounting and terminal settlement so obsolete workers cannot
  finalize a replacement's run. Preserve the costs of both attempts without
  double charging.
- **Acceptance:** cancel during a delayed response with known usage; its cost
  remains charged. Repeat with lease takeover and verify both attempts' usage
  survives exactly once and only the current owner controls terminal settlement.

### F44 — Bind incremental budget claims to explicit hourly allocations

- [ ] **Priority:** P2. **Follow-up:** F30/T26.
- **Locations:** `shared/usage.py:193–211`, `:133–143`.
- **Failure:** `claim_budget` only updates the current hourly row. Across an
  hour boundary that row may not exist until another request arrives, so an
  admitted run is refused despite an unused new allowance. If another request
  creates it, new claims succeed but finalization skips reconciliation because
  the run began in an earlier hour. Behavior depends on unrelated traffic.
- **Required fix:** track the bucket owning each allocation, create/claim it
  atomically under the chosen hourly policy, and reconcile the right allocation.
- **Acceptance:** a single run crosses the hour with and without another
  admission. Equal available budget produces equal continuation and accurate
  settlement; retries and delayed finalization cannot move refunds across hours.

### F45 — Include the unsummarized tail before the first compaction too

- [ ] **Priority:** P2 memory correctness. **Follow-up:** F16/T19.
- **Locations:** `agent/history.py:103`; `pipeline/compaction.py:28,77–80`.
- **Failure:** expanded replay is conditional on `summary_through` existing.
  A new thread still replays only its latest 20 messages, while first compaction
  waits until 60. Messages disappear from context at positions 21–59 before
  any summary contains them. The between-compactions repair misses startup.
- **Required fix:** treat no summary as an entirely unsummarized range, using the
  same bounded replay/compaction policy and explicit truncation handling.
- **Acceptance:** a constraint in message 1 remains represented at messages
  21–59 and across the first summary boundary. Test an ordinary fresh thread,
  not only one preseeded with a summary cursor.

### F46 — Make installed Node dependencies resolvable by ESM

- [ ] **Priority:** P1 for advertised Node execution. **Follow-up:** F06/T08.
- **Location:** `agent/sandbox.py:400–407`, shared dependency mount/environment.
- **Failure:** packages live in `/deps/node_modules`; execution relies on
  `NODE_PATH`. Node ESM imports ignore that mechanism and resolve from workspace
  ancestors. A successful install therefore still leaves modern Node apps and
  previews failing with `ERR_MODULE_NOT_FOUND`.
- **Evidence:** isolated no-network Node check with a workspace and sibling
  dependency directory: CommonJS require returned 42, while ESM import failed.
- **Required fix:** expose dependencies where normal module resolution expects
  them, such as a protected `/workspace/node_modules` mount, consistently for
  commands and previews. Preserve read-only dependency protection.
- **Acceptance:** the same installed fixture runs both CommonJS and ESM imports
  through real execution and preview paths. Do not use only an executable found
  through PATH as proof of Node environment correctness.

### F47 — Keep the active run visible when a steering submission fails

- [ ] **Priority:** P2 UX regression. **Follow-up:** F09/T11.
- **Locations:** `frontend/src/screens/GroupRoom.tsx:425,652–679`.
- **Failure:** composer release now permits steering during a run, but a failed
  steering POST unconditionally clears `aiTyping`. The original stream remains
  healthy while its partial response and STOP control disappear. Later text
  frames update pending text without restoring that flag.
- **Required fix:** preserve active-follow visibility independently of a failed
  message submission. Restore the draft and show its error without implying
  the existing run stopped.
- **Acceptance:** hold a live stream, fail the second POST, and verify partial
  output, active-run status, and STOP remain visible and functional. Retry the
  correction without duplicating the original run.

### F48 — Remove obsolete staged Node manifests before rebuilding

- [ ] **Priority:** P2 reproducibility. **Follow-up:** F06/T08.
- **Location:** `pipeline/repo_deps.py:321,332–334`.
- **Failure:** rebuild removes installed packages but retains staged manifests
  and configuration. Files absent from the new checkout are never removed from
  `/deps`. Deleted `.npmrc`, shrinkwrap, or lockfiles can continue influencing
  installation despite the changed environment fingerprint.
- **Required fix:** clear the staged manifest/configuration set before copying
  the current set, or build in a clean generation and switch after success.
  Preserve unrelated owned resources deliberately; do not blanket-delete paths.
- **Acceptance:** build twice against one environment, removing a previously
  present shrinkwrap/config file before the second build. The second install
  uses only current repository inputs and matches a clean build's resolution.

**Review disposition:** retain F20 and the linked repaired items as partial until
the cases above pass. F35–F38 record the previous review's still-unmodified
deployment/backup defects. Do not confuse current test counts with a production
restore, real proxy deployment, or complete sandbox-provider integration.

## Follow-up review — 2026-09-09, pinned to `3b48448`

Reviewed the claimed fifteen closures in `c8f03c6..3b48448`, including
`e3a04d1`, `8ff80c9`, `56c765d`, and `6fd0848`. These are follow-ups to existing
IDs, not a second set of duplicate tickets. **Do not mark all fifteen closed.**
The six gaps below remain actionable at this commit.

Validation: pinned-source tracing plus isolated checks of HTTP URL parsing,
dependency hashing, and mocked database-client fallback. No production calls or
shared database suites were run. Claude's reported 1609 backend / 229 frontend
passes were not independently rerun in this review.

### F20 reopened — The authenticated object must be the fetched object

- [ ] **P1 — confidentiality.** `shared/storage.py:49,165–176`.
- Ownership now correctly compares the document uploader with Storage's owner
  and checks the team prefix. However, `canonical_path` accepts `?`, and the
  privileged GET interpolates the validated object name into a raw URL.
- A member can upload their own `<team>/restricted.txt?owned` object and file
  its document row. The literal decoy passes ownership checks. The GET instead
  requests `<team>/restricted.txt`, with `owned` as its query, using the service
  key. A known restricted same-team object can therefore be ingested through
  the member's own document. The unique document-path index does not prevent
  this because the two literal names differ.
- Evidence: isolated `httpx.URL` construction produced a request path ending
  in `/restricted.txt` and `query=b'owned'`. Supabase's object-key validator
  permits `?`: https://raw.githubusercontent.com/supabase/storage/master/src/storage/limits.ts
- **Fix:** encode the validated object key as URL path data, preserving `/`
  separators, so the authorized name and fetched name remain identical.
  Handle percent signs and other URL metacharacters consistently too.
- **Acceptance:** ownership-check a member-owned decoy and capture the actual
  outgoing request; it must fetch that literal object or reject it, never the
  restricted target. Exercise the complete downloader, not just its DB gate.

### F35 reopened — Invoke the probe through the actual SSM entrypoint

- [ ] **P1 — deployment.** `scripts/deploy_host.sh:146–154` and
  `.github/workflows/deploy-pilot.yml:40`.
- The workflow pipes the script into `sh -s <sha>`. In this invocation `$0` is
  `sh`, so `$(dirname "$0")/proxy_check.sh` resolves to `/opt/comrade/proxy_check.sh`.
  The committed helper lives at `/opt/comrade/scripts/proxy_check.sh`. Even
  with `COMRADE_HOST` exported, the healthy release fails after activation.
- Separately, a host configured only through Compose's `.env` reaches the new
  unset-host failure. Compose's interpolation does not export `COMRADE_HOST`
  into the parent SSM shell, and the workflow/script does not load it there.
- **Fix:** resolve the helper from the checked-out repository and obtain the
  configured hostname through the deployment's actual configuration path.
- **Acceptance:** run the exact stdin invocation used by the workflow, with
  hostname configured only in `.env`, using harmless command doubles or an
  isolated Compose host. A healthy probe must run successfully. Keep the real
  TLS tests; testing the helper alone does not test this integration.

### F36 follow-up — Restore the application migration ledger

- [ ] **P1 — recovery.** `scripts/backup.py:80,257–259`;
  `server/app.py:357–365`; `shared/migrations.py:15–29`.
- `ON_ERROR_STOP` now preserves SQL failure signals. However, narrowing the
  dump to `public`, `auth`, and `storage` drops
  `supabase_migrations.schema_migrations` and its applied-version records.
- On a fresh recovery target the ledger is missing or unpopulated. Readiness
  cannot confirm the restored schema, and the migration runner either fails
  reading the missing relation or tries to reapply migrations whose objects
  were already restored. A clean psql exit is not a working recovered service.
- **Fix:** include the application migration metadata in the recoverable set,
  or implement an explicit equivalent restoration of the exact saved state.
  Do not blindly mark every migration applied on the destination.
- **Acceptance:** restore onto a newly provisioned target, start the actual
  application, pass readiness, and apply a subsequent migration exactly once.
  Row/RLS checks alone do not prove application recovery.

### F37 reopened — Localhost is not proof of database identity

- [ ] **P1 — wrong restore target.** `scripts/backup.py:155–165`.
- Refusing nonlocal hosts is an improvement, but every localhost port is still
  rewritten to port 5432 inside `COMRADE_DB_CONTAINER`. `localhost:6543` can be
  another database or an SSH/SSM tunnel to a remote database; neither is the
  selected local Supabase container. A restore can still operate on the wrong
  server if credentials and database name match.
- Evidence: forcing only the missing-client-binary branch with mocked
  subprocess calls changed requested `localhost:6543` to `127.0.0.1:5432`.
  No database command was actually executed.
- **Fix:** make container targeting explicit or verify the selected container's
  published endpoint matches the requested endpoint; reject unverified
  mappings. Never infer server identity from loopback hostname alone.
- **Acceptance:** absent client binaries, another local port and a loopback
  tunnel must preserve the intended server or fail before executing psql.
  Also retain coverage of the explicitly supported local-container route.

### F48 reopened — Hash the inputs whose removal should trigger cleanup

- [ ] **P2 — stale dependencies.** `pipeline/repo_deps.py:281,322–343`.
- Cleanup now removes staged Node manifests, but the environment key omits
  `.npmrc`, `npm-shrinkwrap.json`, and `pnpm-workspace.yaml`. Changing or deleting
  only these leaves the marker unchanged. The installer exits as already
  current before reaching cleanup, so existing installations/configuration
  remain stale. The recipe-version bump fixes one upgrade, not future edits.
- Evidence: isolated execution of the pinned hashing functions returned the
  same digest before and after removal of these inputs.
- **Fix:** include every staged Node input that affects installation in the
  environment key, reusing `NODE_MANIFESTS` rather than a divergent file list.
- **Acceptance:** change/remove each input independently without changing
  package.json or a currently hashed lockfile. The key must change and a second
  install must rebuild using only the current inputs.

### F43 reopened — Settlement must retain worker ownership after completion

- [ ] **P1 — budget accounting.** `shared/usage.py:131–156`;
  `agent/runtime.py:84–103`; `shared/agent_runs.py:133–155`.
- The new terminal-status guard blocks a stale worker while its replacement
  is running. It does not distinguish owners once the replacement finishes.
  `finish_run` commits status and totals in a separate transaction from
  `finalize_usage`; `_finish` catches an ownership failure and still submits
  its local totals for settlement.
- Source-confirmed interleaving: replacement commits completion with 1,200
  tokens; stale worker fails its fenced finish but settles 50 tokens while
  the run is now terminal; replacement settlement then skips the already-set
  `usage_finalized_at`. The bucket permanently records the stale total and
  releases budget that was actually spent. No concurrent DB test was run in
  this review.
- **Fix:** settle within the fenced completion transaction or enforce durable
  settlement ownership. Preserve legitimate cancellation settlement explicitly;
  terminal status alone cannot establish which worker's totals are authoritative.
- **Acceptance:** force the interleaving above and assert the final bucket
  reflects the replacement's 1,200 tokens. Also retain the cancellation and
  stale-worker-while-replacement-running cases.

**Disposition:** direct object alias checks, independent heartbeats, the PR-link
repair job, real Node module mounting, and strict SQL error handling are useful
repairs. The remaining issues above cross their integration boundaries.
F02/F17 and the previously deferred acceptance checks remain separate open work.

## Fourth review — pinned to `bba661d` (`6b421d1` repair)

Reviewed all six repaired application paths and their new regression tests in
`3b48448..bba661d`. F20's URL encoding, F36's inclusion of the migration ledger,
and F48's shared list of hashed/staged inputs address the reported defects.
F35's stdin-relative helper path is also corrected. Three remaining product
gaps and one test-harness defect follow; this is not a claim of a fresh full
production acceptance pass.

### F43 remains open — Cancellation erases the settlement fence

- [ ] **P1 — budget accounting.** `shared/usage.py:156–166`,
  `agent/run_queue.py:111–118`, `agent/runtime.py:84–107`.
- The replacement-completes case is now fenced correctly. Cancellation still
  sets the stored worker ID to NULL, and the new settlement predicate expressly
  accepts ANY caller when that stored ID is NULL. It cannot identify the last
  legitimate owner after a replacement is cancelled.
- Reproduction: old worker loses lease, replacement incurs 1,200 tokens, member
  cancels replacement, old worker's exit settles 50, replacement settles 1,200.
  The stale worker wins `usage_finalized_at`, permanently leaving 50 in the
  bucket. The cancelled-run test supplies an arbitrary worker ID and therefore
  tests acceptance without proving ownership.
- Evidence: executed the real `finalize_usage` twice against an isolated
  in-memory SQL adapter (Postgres placeholders/functions adapted for SQLite).
  With a cancelled NULL-owner run, stale=50 followed by replacement=1,200 left
  the bucket at **50**. Source tracing establishes how cancellation creates
  that state. No shared PostgreSQL mutation or concurrency test was run.
- **Fix:** preserve the last legitimate settlement identity when cancellation
  revokes execution, or settle from an authoritative durable usage record.
  Do not make NULL ownership permission for every obsolete worker to settle.
  Keep queued-never-executed cancellation distinct from active execution.
- **Acceptance:** replacement cancelled, stale worker settles first, rightful
  worker settles second: the stale call must not finalize the run. Also keep
  ordinary completion, cancellation without recovery, and queued cancellation
  working.

### F37 remains open — Check the bind address as well as the port

- [ ] **P1 — wrong restore target.** `scripts/backup.py:132–152,194–211`.
- `_publishes` checks only the number following the last colon. A container
  bound to `127.0.0.2:54322` passes a request for `127.0.0.1:54322`. Those are
  different endpoints; a separate database or tunnel can occupy the latter.
  Execution still rewrites the request to the selected container's own server.
- Evidence: mocked Docker returned its actual single-port output shape,
  `127.0.0.2:54322`. A requested `127.0.0.1:54322` reached `docker exec` rather
  than being refused. No real database/Docker mutation was performed.
- Docker explicitly supports publication on a specific host address:
  https://docs.docker.com/engine/network/port-publishing/
- **Fix:** verify the complete requested endpoint against the container's bind
  address, including wildcard and IP-family semantics. Fail closed when the
  mapping cannot be established; explicit container mode is also acceptable.
- **Acceptance:** same port/different loopback address and differing address
  families must not silently choose another server. Matching loopback and
  genuinely covering wildcard bindings should retain their supported behavior.

### F35 follow-up — Read Compose's resolved hostname

- [ ] **P2 — false deployment failure.** `scripts/deploy_host.sh:165–176`.
- The hand-written `sed`/`tr` parser does not implement Compose's `.env` syntax.
  With `COMRADE_HOST=comrade.example.test # public hostname`, Compose configures
  the correct host, but the release passes the literal comment and spaces to
  the probe. Variable interpolation and trailing whitespace diverge too.
  This check occurs after activation, so a healthy release is reported failed.
- Evidence: executed the exact committed parser block in Git Bash against an
  isolated `.env`; output was `'comrade.example.test # public hostname'`.
  Compose documents inline comments, whitespace handling, and interpolation:
  https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/
- **Fix:** obtain the effective hostname from Compose's resolved configuration
  or environment, without printing unrelated configuration/secrets. Reuse the
  parser the deployment already relies on instead of extending a second one.
- **Acceptance:** the stdin release invocation and Compose agree for plain,
  quoted, commented, interpolated, and shell-overridden hostname values.

### Deployment-test harness — Windows command doubles do not intercept Git

- [ ] **P2 — validation reliability.** `tests/test_deploy_host_script.py:59–73,
  149–195`.
- The tests prepend their command-double directory to the Windows environment
  PATH, but this host's Git Bash puts `/mingw64/bin` before it. `git` resolves
  to the real binary, while `flock` resolves to the double. The script performs
  a real fetch of `abc123`, fails there, and never exercises the intended gate.
- Independent run of the backup/deploy files: **26 passed, 1 skipped, 7 failed**.
  Deployment errors included `fatal: couldn't find remote ref abc123`.
  Four parameterized failure cases passed at that unrelated early failure
  because they only assert nonzero exit and absence of activation.
- Confirmed using `type -a git` in the same fake-host environment: real Git
  appeared before the fake Git. Application working-tree files remained clean.
- **Fix:** establish command-double precedence inside the launched shell and
  assert resolution before invoking the script. Each negative case must assert
  that its intended stage was reached and supplied the expected failure.
- **Acceptance:** the documented Windows invocation runs without external Git
  access; all intended failure stages execute, and unrelated early failures
  cannot satisfy those tests. Also retain the Linux deployment lane.

**Other validation:** 11 selected dependency hash/cleanup tests passed; eight
isolated full-downloader URL cases passed with ownership and HTTP stubbed.
The restore drill and full backend/frontend suites were not rerun. Restoring
the migration ledger is fixed in code, but fresh-cluster application readiness
and the next migration remain acceptance work, not established by a row-set
assertion alone. F02/F17, F06 egress acceptance, F46's upgrade window, and the
previously deferred A-items remain separate open work.

## Fifth review — pinned to `f474b48`

Reviewed `c735dbc`, `6de127e`, `a1dbf47`, and `417bb54` against `bba661d`.
The reported stale-worker-after-cancellation case is addressed by the new
settlement owner. The Windows command-double problem is independently verified
fixed on the host where the previous review reproduced it: running
`tests/test_backup_integrity.py tests/test_deploy_host_script.py` returned
**53 passed, 1 skipped**. No shared database suite or deployment was run.

Two of the four repairs still have gaps. One additional accounting lifecycle
gap is listed separately as F49; it should not be described as proof that the
specific stale-worker regression remains unfixed.

### F35 remains open — Extract Caddy's hostname, not the first matching key

- [ ] **P1 — deployment probe targets the wrong site.**
  `scripts/deploy_host.sh:186–188`;
  `tests/test_deploy_host_script.py:558–572`;
  `docker-compose.yml:19`; `docker-compose.prod.yml:15–16`.
- Compose emits `COMRADE_HOST` for several services. The API inherits the
  literal `.env` value through `env_file`; Caddy receives the interpolated
  `${COMRADE_HOST}` value, where the shell override applies. The new `awk`
  stops at the FIRST matching key, which is the API's, not Caddy's.
- **Real Compose reproduction**, using temporary copies of both committed
  Compose files and no running service changes:
  - `.env`: `COMRADE_HOST=from-dotenv.test`;
  - process environment: `COMRADE_HOST=from-shell.test`;
  - resolved `services.caddy.environment.COMRADE_HOST`: `from-shell.test`;
  - resolved API hostname: `from-dotenv.test`;
  - committed release extraction: `from-dotenv.test`.
- The new test repeats the same first-key search as the implementation. Its
  assertion therefore passes while the probe disagrees with Caddy. The claim
  that Compose 2.39.4 reverses shell interpolation precedence comes from
  measuring the wrong service; correct that comment and the progress ledger.
- **Fix:** select `services.caddy.environment.COMRADE_HOST` from Compose's
  resolved model, rather than matching an unqualified YAML key. Preserve
  command failures and avoid printing the rest of the configuration.
- **Acceptance:** assert the exact Caddy value under conflicting `.env` and
  shell values, then prove that value reaches the probe through the complete
  stdin deployment invocation. Keep the quoting/comment/interpolation cases.

### F37 remains open — A one-family wildcard does not disambiguate localhost

- [ ] **P1 — wrong restore target.** `scripts/backup.py:200–204`.
- Literal-address matching is corrected. The hostname branch still accepts
  any matching-port wildcard without resolving the requested hostname or
  establishing that every candidate belongs to this container.
- Example: selected container publishes IPv4 `0.0.0.0:54322`, while a separate
  database or tunnel listens on IPv6 `[::1]:54322`. `localhost` can connect to
  the latter; fallback instead executes inside the IPv4 container.
- Evidence: on this host `getaddrinfo('localhost', 54322)` returned `::1`
  before `127.0.0.1`. With mocked publication `0.0.0.0:54322`, the real
  `_publishes('localhost', '54322')` returned True, while the same function
  correctly rejected the literal `::1` request. No database connection was
  attempted; the unresolved-name acceptance is directly reproduced.
- **Fix:** require an unambiguous literal endpoint for fallback, or resolve
  and validate the full candidate set before mapping it to a container.
  A wildcard covering one family does not establish ownership of the other.
- **Acceptance:** dual-family localhost with only one family published must
  fail closed or demonstrably preserve the requested server. Retain exact
  address, same-family wildcard, different-address, and different-port tests.

### F49 — Settle cancelled parked runs from their durable usage

- [ ] **P2 — quota remains reserved after STOP.**
  `agent/runtime.py:434–443`; `shared/agent_runs.py:195–208`;
  `server/app.py:764–768`; `shared/usage.py:170–173`.
- A permission wait checkpoints usage, clears execution ownership, and returns
  from the runtime. There is no worker left to settle later. The cancellation
  route calls settlement only for a previously queued run; cancelling
  `waiting_for_permission` sets terminal status and returns without settlement.
- `usage_owner` correctly preserves the former worker's identity, but identity
  alone does not create a settlement caller. A 6,000-token reservation can
  remain charged after a paused run that spent only 1,200 is cancelled,
  unnecessarily exhausting the team's remaining admission budget.
- Evidence: the real cancellation handler body, with auth/DB collaborators
  stubbed to a parked run, returned `cancelled` and made zero settlement calls.
  Source tracing confirms the worker already returned and the checkpointed
  input/output totals are available. This is an adjacent lifecycle omission,
  not a reproduction of the now-fixed stale-worker race.
- **Fix:** provide an authorized terminal settlement path for work that is
  durably parked and no longer executing, using its checkpointed totals.
  Preserve worker fencing for active/in-flight calls and distinguish a resumed
  queued run from a run that never executed. Read/transition/settlement must
  not race a new claim or resume.
- **Acceptance:** run to permission wait with known usage, let the worker
  return, cancel through the API, and verify the reservation is reconciled
  exactly once to actual usage. Also exercise cancellation after approval has
  requeued the run but before its next claim.

### Validation record correction — Full versus quick gates

The progress ledger says default `scripts/gates.sh` exited zero with no API
and skipped browser journeys. At this pinned commit `QUICK=0` by default,
`--quick` explicitly skips that lane, and lines 125–135 exit **1** if the API
is unavailable. The described outcome does not match the committed default
path. Record the exact invocation and preserved output (including any wrapper)
rather than asserting that this script silently permits a missing API.
Do not infer a full browser gate pass from the reported exit code. The disclosed
realtime retry also remains an unresolved reliability observation.

**Disposition:** Windows harness verified here; the specific F43 ownership race
looks repaired by source inspection, without an independent PostgreSQL rerun.
F35 and F37 remain partial; F49 is additional work. Previously deferred Box,
egress, upgrade-window and A-item acceptance is unchanged.

## Sixth review — pinned to `87d2029`

Reviewed `a63488f` (F35), `023cae1` (F37), and `87d2029` (F49) against
`f474b48`. Claude's progress document has uncommitted edits; this review leaves
those edits alone and pins application conclusions to the commits above.

**F35 and F37 address the reported defects.** The release now asks the running
Caddy container for its own hostname, checks that command's exit status, and
does not select another service's environment. Database fallback now requires
every resolved address to match a published address or a same-family wildcard.
Focused independent verification:

`pytest -q -p no:cacheprovider tests/test_backup_integrity.py tests/test_deploy_host_script.py`

Result: **57 passed, 1 skipped**, including real Compose configuration and an
ephemeral Caddy-image `printenv` check. This does not establish a real pilot
deployment or the full gate outcome. No shared PostgreSQL suite was run.

F49 fixes the ordinary parked-cancellation and approval-requeued cases, and
returns the pre-cancellation status under the same row lock as cancellation.
Two remaining failure paths are below.

### F49 follow-up — Do not finalize an incomplete lease-recovery checkpoint

- [ ] **P1 — paid usage can be discarded.** `server/app.py:769–778`;
  `shared/usage.py:221–236`;
  `supabase/migrations/20260907120000_permission_continuation.sql:38–44`;
  `agent/runtime.py:419–427`.
- `queued` does not mean the old execution finished. Lease recovery requeues
  an expired run and clears `worker_id` while the old worker may still have
  an in-flight model response. It does not checkpoint that response's usage.
- If the member cancels during this recovered-queued interval, the new route
  calls `settle_from_checkpoint`. Terminal status plus NULL worker ID accepts
  the row and finalizes its old checkpoint (possibly zero). When the paid
  response arrives, the previous owner's normal exit attempts settlement but
  loses to the already-written `usage_finalized_at`.
- Evidence: executed the real checkpoint and worker settlement functions in
  sequence against an isolated in-memory SQL adapter. A cancelled recovered
  run with `usage_owner='old-worker'`, 6,000 reserved and a zero checkpoint
  settled to **0**; the subsequent rightful-worker settlement of **1,200**
  left the bucket at **0**. Source tracing establishes the recovery/cancellation
  path to that state. This was not a live model or PostgreSQL concurrency test.
- **Fix:** distinguish a complete permission-wait checkpoint from a lease-
  recovered execution whose final usage is unknown. Retain a safe reservation
  or provide later authoritative reconciliation; do not declare the checkpoint
  final merely because cancellation cleared execution ownership.
- **Acceptance:** hold a model response, expire/recover the lease, cancel
  before a replacement claims the run, then release the paid response. Its
  usage must be accounted for. Keep ordinary parked and approval-requeued
  cancellation working, and retain stale-worker ownership fences.

### F49 follow-up — A failed settlement must be recoverable after cancellation

- [ ] **P2 — STOP can leave the reservation stranded.**
  `agent/run_queue.py:123–139`; `server/app.py:769–779`.
- Cancellation commits in `cancel_run` before `settle_from_checkpoint` opens
  its transaction. If that second operation fails, the run is durably cancelled
  but still reserved. Retrying STOP returns `already_finished=True`: `cancel_run`
  now returns None and the route skips settlement. No worker remains for a
  permission-parked run, and no durable retry is recorded.
- Evidence: fault injection into the real handler body, with auth/DB
  collaborators stubbed. First call committed cancellation then raised a
  temporary settlement error; second call returned `already_finished=True`.
  Settlement was attempted **once total**, so the retry could not repair it.
- **Fix:** commit eligible checkpoint settlement with cancellation, or make
  the pending settlement durable and safely retryable for this exact terminal
  transition. Do not blindly settle every cancelled run on retry: active and
  lease-recovered executions may still have unaccounted usage, as above.
- **Acceptance:** fail the settlement write after cancellation, retry the
  request, and prove the completed checkpoint is reconciled exactly once.
  A process interruption at the same boundary must also have a recovery path.

**Disposition:** F35/F37 reported cases verified locally; real deployment
acceptance remains pending. F49 is partial on the two paths above. The gate
exit-status retraction is correct; no final full-gate result was independently
established in this review. Earlier Box, egress, dependency-upgrade, and A-item
acceptance remains separate work.

## Seventh review — pinned to `61f7fc0` (`b205193` repair)

**The two sixth-review F49 paths are fixed and verified locally.** Cancellation
and eligible settlement now share a transaction; a failed settlement rolls the
terminal transition back. Complete permission-wait checkpoints are explicitly
marked, new claims invalidate them, and lease-recovered runs retain their
reservation for the worker's eventual usage. Already-terminal retries attempt
eligible repair without bypassing those checks.

Independent execution, after checking no competing pytest/gate/worker process
was running and confirming the DB endpoint was local `127.0.0.1:54322`:

`pytest -q -p no:cacheprovider tests/test_run_cancellation.py tests/test_usage_continuity.py tests/test_agent_resume.py tests/test_permission_continuation.py`

**58 passed**, with deprecation warnings only. These ran against the local
PostgreSQL stack and cover rollback/retry, recovered-run late settlement,
permission waits and resumes. The full gate was not independently rerun.
One additional upgrade-state gap follows, distinct from the now-passing new-run
cases above.

### F50 — Unknown legacy ownership is not proof a run never executed

- [ ] **P2 — incomplete usage may be finalized during upgrade.**
  `shared/usage.py:263`;
  `supabase/migrations/20260909130000_settlement_ownership.sql:24–25,48–51`;
  `supabase/migrations/20260909160000_settlement_checkpoint.sql:53–57`.
- Both new columns are nullable and deliberately unbackfilled. An existing run
  that executed and lost its lease before these migrations can therefore have
  `attempts > 0`, `worker_id=NULL`, `usage_owner=NULL`, and no checkpoint marker.
  The NULL owner here means unknown historical ownership, not never executed.
- The new settlement predicate accepts `usage_owner is null` independently of
  the checkpoint marker. Cancelling/retrying such a legacy recovered run can
  finalize its stale zero checkpoint and release the entire reservation. This
  contradicts the migration's statement that unknown existing state will keep
  its reservation held.
- Evidence: the actual `settle_cancelled` function was executed against an
  isolated SQL adapter with a legacy-shaped executed run (`attempts=1`, both
  added columns NULL, 6,000 reserved, checkpointed tokens zero). It set the
  finalization marker and reduced the bucket to **0**, rather than holding the
  6,000 reservation. This reproduction did not run a full pre-upgrade migration
  sequence against PostgreSQL; source inspection establishes that unbackfilled
  legacy rows receive precisely these NULL values.
- **Fix:** require positive durable evidence that a run never executed, or
  distinguish legacy/unknown ownership explicitly during migration. Do not use
  a newly added NULL column as that evidence. Keep unknown/incomplete legacy
  executions conservatively reserved until their usage can be established.
- **Acceptance:** create an executed, lease-recovered run in the pre-owner
  schema, apply both migrations, cancel through the API, and verify its unknown
  usage is not finalized as zero. Retain full refunds for genuinely never-
  claimed runs and exact settlement for newly completed permission checkpoints.

**Disposition:** the reported F49 regressions pass independently; F50 remains
an upgrade acceptance fix. Claude's recorded full gate includes browser
journeys but skips real GitHub without credentials. From-empty migration and
actual deployment acceptance remain unverified here; earlier Box/egress and
other deferred work is unchanged.

## Eighth review — pinned to `6c6387f` (`54fa8ce` repair)

**F50 is fixed and independently verified.** Settlement now requires durable
evidence of no prior claim (`attempts=0`, no owner), or an explicitly complete
checkpoint. The legacy executed case holds its reservation; genuinely
never-claimed legacy work still refunds it.

Verification:

- **59 passed, 2 deselected** across cancellation, usage continuity, resume and
  permission-continuation tests. The two new shared-schema upgrade tests were
  deliberately excluded for the F51 reason below. Checked first that no
  competing suite/worker was active and that the configured DB was local.
- Independently ran both migration bodies and the actual settlement function
  against isolated PostgreSQL temporary tables, with only object references
  redirected to `pg_temp`. A pre-upgrade executed row retained **6,000** tokens;
  a pre-upgrade never-claimed row refunded to **0**. All temporary work rolled
  back; application schema and unrelated rows were not modified by this check.

### F51 — Upgrade tests erase settlement metadata from the shared database

- [ ] **P1 — developer data integrity / test isolation.**
  `tests/test_run_cancellation.py:537–565` (`pre_upgrade_schema`).
- This fixture opens `settings.comrade_db_url_admin` with autocommit enabled,
  drops both settlement triggers, and drops `usage_owner` and
  `usage_checkpoint_at` from the actual `public.agent_runs` table. This affects
  every run in the configured database, not just the seeded test team's rows.
  It is included in ordinary pytest/default gates, not an explicit reset lane.
- Reapplying the migrations restores column definitions and triggers but
  cannot restore their previous values. Existing settlement ownership and
  complete-checkpoint markers become NULL permanently. A process interruption
  can additionally leave the schema missing these columns/triggers.
- Evidence: reproduced the same drop/reapply sequence on temporary PostgreSQL
  tables containing a sentinel row with a known owner and checkpoint. Both
  values became NULL after the exact migration bodies recreated the columns.
  The destructive shared-table fixture itself was not run in this review.
- **Fix:** run schema-changing upgrade acceptance against a dedicated disposable
  database or otherwise fully isolated schema/transaction. Do not mutate the
  configured application database's schema from a normal fixture. Restoring
  definitions is insufficient; preserve unrelated rows and their values.
- **Acceptance:** place an unrelated sentinel run with non-NULL settlement
  metadata in the normal development database, execute the isolated upgrade
  test, and assert its row and schema are unchanged. Inject test failure too.
  The isolated legacy-upgrade and never-claimed refund assertions must still
  pass through real migrations and settlement logic.

**Disposition:** no additional application defect found in this F50 repair.
Move the new upgrade tests off the shared database before running the ordinary
full gate again. Previous production acceptance, missing real-GitHub evidence,
and other deferred work remain unchanged.

## Ninth review — pinned to `26b46f5` (`b3bbca3` repair)

**F51 is fixed and independently verified locally. No new actionable code
finding in this change.** The shared-schema fixture was removed. Upgrade DDL
now runs against a distinct database restored from a real backup; cancellation
route coverage remains in the ordinary tests without schema changes.

After confirming no competing suite/worker was active, that the configured DB
was local and distinct, and that `comrade_settlement_upgrade` did not already
exist, independently ran:

`pytest -q -p no:cacheprovider tests/test_settlement_upgrade.py`

**5 passed in 8.57 seconds.** Captured the configured database's settlement
column/trigger definitions and unrelated runs' settlement values before the
test module and compared them after: unchanged. The module's sentinel tests
also preserved non-NULL settlement metadata through success and injected
failure. No disposable test database remained afterward. Implementation files
were unchanged by the review.

The F50 legacy-hold and never-claimed-refund checks still pass after isolation.
This verification closes the specific shared-schema/data-loss mechanism
reported in F51. It is not an independent rerun of the full gate or proof of
production deployment, clean migration-chain bootstrap, or real GitHub work.

Historical-data caveat: observing zero non-NULL ownership values AFTER earlier
destructive fixture runs cannot by itself prove those runs lost no values.
That claim needs a pre-run snapshot/backup or equivalent historical evidence.
The current repair prevents the mechanism going forward; this review does not
establish whether earlier runs caused loss.

**Disposition:** F51 locally closed. Proceed with the remaining deployment and
other deferred acceptance work; no further implementation fix requested by
this review.


## Hackathon release preflight — 2026-09-09

Candidate: `26b46f5`. Live EC2: `5c73fd079243a772958e55ce945f844768a42fad`.

**Decision: hold the full release for concrete repository-execution prerequisites,
not the whole production-readiness backlog.** Public login renders and current
`/api/ready` answers ready. Live queues had no pending/running work at inspection.

Confirmed through read-only SSM probes on `i-0e5e97d751ffbd262`:

- The running agent worker cannot execute `docker`: executable not found. The
  candidate app image adds the CLI. The host socket group is **113**, while the
  candidate Compose default is **999** and `COMRADE_DOCKER_GID` is unset in the
  live API environment. Set/verify the worker group against the actual socket
  before activation; prove daemon access from both new worker containers.
- The existing `comrade-sandbox:latest` has Python but no Node/npm on PATH. The
  candidate sandbox Dockerfile adds them, but `scripts/deploy_host.sh` builds
  Compose services only, not this separate sandbox image. Rebuild and verify
  the sandbox before promising JavaScript repository execution.
- Neither `COMRADE_SETUP_PROXY_URL` nor `COMRADE_SETUP_PROXY_CONTAINER` is set;
  only the five application containers are running. The candidate's
  `agent/sandbox.py:722` refuses dependency installation in this state. Provision
  the restricted registry proxy and exercise a real locked dependency install
  through `run_setup`, followed by `repo_run`. Preserve denied access to host,
  metadata, platform services and arbitrary destinations. Do not bypass the
  network guard to make the demo pass. This is the concrete deployment impact
  of the still-open F06 acceptance, rather than a new unrelated requirement.

Previews are disabled and live backend is explicitly Docker, so the absent Box
integration alone is not a blocker for a demo using Docker with previews off.
Recheck existing dependency volumes against F46 before enabling repository work.
No release, production configuration change, or production migration was made by
this preflight. These findings are live observations, not inferred from the
older review checkboxes.


### F52 — A passing live-agent journey did not produce an answer

- [ ] **P1 — hackathon usefulness acceptance / diagnosis required.**
- Independently ran the full `scripts/gates.sh` at `26b46f5` against the local
  stack, with no competing suite/worker present at start. Captured the actual
  script status: **GATE EXIT 0**. Backend: **1692 passed, 8 skipped, 17
  deselected**; frontend component: **229 passed**. Real GitHub: **2 skipped**.
- During the real-model browser journey, the ordinary question **"What tasks
  are open right now?"** exhausted **three empty-response attempts**, and
  `agent/runtime.py` finalized the run as failed with an explanation rather
  than an answer. The journey still passed: `frontend/tests/e2e/journeys.spec.ts:
  148–150` deliberately accepts either an AI message or `[data-agent-note]`.
  That is valid coverage for failure feedback, but it does not establish the
  primary user outcome needed for judging.
- The same run logged a ContextVar reset error during async-generator shutdown
  (`shared/observability.py:54` via `agent/runtime.py:272`). Its relationship to
  the empty responses is NOT established; do not claim it caused them.
- **Required before demo sign-off:** reproduce/diagnose the empty outcome using
  the actual configured model and tool path. Verify useful answers to a small
  set of representative judge prompts (task lookup, document question, team
  context) in a designated test team, and exercise a consent action where
  relevant. Preserve the existing error-feedback assertion as separate
  coverage. Do not conceal empty responses, fabricate a successful answer, or
  make an error notice count as agent-usefulness acceptance.
- **Evidence boundary:** this failure was observed against the local configured
  model/stack, not a signed-in production judge account. Production login UI
  and readiness were checked, but a useful production agent answer has NOT
  been demonstrated by this review. Full local log: `.codex-judge-gates.log`.

**Updated release decision:** hold deployment for F52 and the concrete Docker,
sandbox-image and registry-proxy prerequisites above. Broader deferred polish
and production hardening need not all be completed before a hackathon release.


## Tenth review — pinned to `beaaa79` (2026-09-10)

**Agent usefulness improved in an independently verified local run. Release
remains held for the registry proxy and real-host execution acceptance.**

- Reviewed `da46ec7`, `b791156`, `2c6a778`, and `26a79e8` and their callers.
- Independently ran `tests/test_observability.py` and
  `tests/test_deploy_host_script.py`: **41 passed in 34.99s**.
- With no competing local suite/worker and after verifying the database host is
  local, ran the four live usefulness tests with a test-only collection plugin
  setting `MAX_ASKS=1`. Application code and the six internal attempts were
  unchanged. **4 passed in 34.28s**. The task, wiki and team-context answers
  contained the seeded facts; the consent test staged an item without creating
  the task. No second user ask was needed. Log: `.codex-review-f52-live.log`.
- This is four successful local cases, not a measured production success rate
  or root-cause resolution of empty candidates. The committed live tests still
  allow two user asks (up to twelve internal attempts); their default invocation
  is opt-in, so default gate success alone remains insufficient for usefulness.
- The release now exports the socket's actual group and builds the default
  sandbox image. Those changes passed local shell tests; neither real-worker
  daemon access nor a production sandbox install/test was verified this round.
- The registry proxy remains explicitly unprovisioned in the implementation
  ledger. F46 volume compatibility is also still pending. The latest successful
  deployment workflow remains the September 6 run at `5c73fd0`; no newer workflow
  deployment was found. No production changes were made by this review.

### F53 — Cross-task logging cleanup restores the wrong context

- [ ] **P2 — observability correctness; regression in the F52 cleanup repair.**
- **Locations:** `shared/observability.py:67–74`,
  `agent/runtime.py:594–631`; tests in `tests/test_observability.py`.
- Replacing `ContextVar.reset(token)` with `_context.set(previous)` removes the
  exception, but writes into whichever task closes the generator. It cannot
  restore the context of the task that entered the generator's scope. This can
  leave subsequent log records carrying stale team/run identifiers and overwrite
  the closing task's independent correlation context.
- **Reproduced without a model or database:** set caller context to
  `caller-team`; advance a generator inside `log_context(team_id='turn-team')`;
  close it from a child task whose own context is `closer-team`. After closing,
  caller remains **turn-team** (expected caller-team), and closing task becomes
  **caller-team** (expected closer-team). Both contexts are wrong although no
  exception is raised.
- The new restoration test checks the outer context only AFTER `asyncio.run`
  returns. That outer context was isolated from the async task already; it does
  not inspect the contaminated driving task and therefore passes this defect.
- **Required fix:** fix generator ownership/lifetime rather than replace token
  semantics globally. The production consumer `run_turn` returns from inside
  its `async for` on empty/permission/cancel/busy outcomes without explicitly
  closing `stream_turn`. Close the generator in its driving task/context on
  every exit (e.g. `contextlib.aclosing` around the consumer), and retain proper
  same-context scoped restoration. If cross-task closure must remain supported,
  scope correlation to execution in its owning task rather than treating a
  value assignment in the closing task as restoration of another task.
- **Acceptance:** assert context inside the original driving task immediately
  after early termination, and inside a distinct closing task with its own
  identifiers. Both retain their own prior context. Cover normal completion,
  early terminal frames and cancellation with no generator-shutdown exception.
  Do not use a surrounding `asyncio.run` boundary to make stale state disappear.

**Disposition:** F52 has stronger local usefulness evidence and a retry
mitigation; its underlying empty-candidate cause remains unknown. F53 needs a
small lifecycle repair. The registry-proxy prerequisite and real installed
repository execution are still the material hackathon deployment blockers.


## Eleventh review — pinned to `5e139e0` (`d9f70f5` repair)

**F53 locally closed. No new actionable defect found in this repair.**

`run_turn` now owns `stream_turn` through `contextlib.aclosing`, and the logging
scope uses `ContextVar.reset(token)` again. Early returns therefore close the
generator before leaving its driving task, rather than relying on async-generator
shutdown in another context.

Independent verification:

- `pytest -q -p no:cacheprovider tests/test_observability.py`: **17 passed in
  7.39s**.
- Exercised the actual `run_turn` consumer with a small stand-in stream holding
  real `log_context` across yields. All **eight** cases closed the stream and
  restored the driving task's exact prior team/run context: busy, empty,
  cancelled, over_budget, waiting_for_permission, final, raised exception, and
  task cancellation. This check required no model, database, or implementation
  edits.

This closes the previously reproduced logging-lifetime defect. The full gate
and live usefulness tests were not rerun in this review; Claude's quoted gate
counts remain separately reported evidence. No production changes were made.

**Release disposition:** prioritize provisioning the restricted registry proxy
and proving a real installed-repository run on the pilot host, including the
new worker Docker-group and sandbox-image setup and F46 volume compatibility.
Those remain the concrete deployment acceptance gaps. The recurring realtime
flake still needs diagnosis; it has not been established here whether it is a
local test-environment issue or a production event-delivery defect.


## Deployment — 2026-09-10, `5c73fd0` → the candidate

The seven prerequisites, each verified on the pilot host `i-0e5e97d751ffbd262`
rather than locally. Live before this was the September 6 run at `5c73fd0`.

### 1. The registry egress proxy · `cda58fc`

`agent/sandbox.py:run_setup` refuses to run unless COMRADE_SETUP_PROXY_URL and
COMRADE_SETUP_PROXY_CONTAINER are both set. The host had neither, so dependency
installation was disabled — the correct failure, and also no repository work.

squid, pinned, built from `docker/registry-proxy.Dockerfile`, configured by
`docker/squid.conf`, run as a Compose service with no published ports. Allowlist:
`pypi.org`, `.pythonhosted.org`, `.npmjs.org` — exactly what the recipes in
`pipeline/repo_deps.py` need, including `pip install uv "poetry>=2"`. Default deny
is the last rule; CONNECT is confined to 443; squid's own manager is refused.

Verified ON THE HOST by `scripts/proxy_egress_check.sh`, with squid's own log as
the evidence rather than my summary of it:

```
TCP_TUNNEL/200  CONNECT pypi.org:443
TCP_TUNNEL/200  CONNECT files.pythonhosted.org:443
TCP_TUNNEL/200  CONNECT registry.npmjs.org:443
TCP_DENIED/403  GET  http://169.254.169.254/latest/meta-data/   <- the real one
TCP_DENIED/403  GET  http://metadata.google.internal/
TCP_DENIED/403  CONNECT example.com:443
TCP_DENIED/403  GET  http://example.com/
TCP_DENIED/403  GET  http://pypi.org.evil.test/
TCP_DENIED/403  GET  http://<a neighbour container>:8080/
TCP_DENIED/403  GET  http://<proxy>:3128/squid-internal-mgr/info
TCP_DENIED/403  CONNECT pypi.org:8443
```

…and with no proxy configured the internal network has no route out at all.

🔴 **The first version of that check proved nothing.** Squid was exiting with
`FATAL: failed to open /var/run/squid.pid: (13) Permission denied`, so the proxy
was dead — and every deny case "passed" on a DNS failure while every allow case
failed for the same reason. The script now refuses to report anything until the
proxy is accepting connections, requires a denial to be squid's own 403, and
proves the client can open a TCP connection to it first.

What it honestly does not do: it matches the hostname a client asks for, so for
HTTPS the policy is which host a tunnel may open to, not what travels inside it.

### 2. Docker access and the sandbox toolchain

```
/var/run/docker.sock            root:docker gid=113   (compose default was 999)
candidate worker image, gid 113 client=29.8.0 server=29.1.3   both roles
candidate worker image, gid 999 permission denied
the RUNNING old workers         no docker CLI in this image
comrade-sandbox                 python 3.12.14  node v22.23.2  npm 10.9.8
                                pnpm 9.15.4  pytest 8.3.4
```

After deployment, from the real containers rather than the images:

```
docker exec comrade-agent-worker-1     docker version -> server=29.1.3
docker exec comrade-pipeline-worker-1  docker version -> server=29.1.3
```

### 3. F46 dependency volumes

Zero `comrade-deps-*` volumes existed on the host, so there was nothing built by
the old layout to migrate and nothing to preserve. F46's code fix — the
`volume-subpath=node_modules` mount — was verified working rather than assumed:
see the ESM import below.

### 4. Repository work, through Comrade's own code · `scripts/repo_execution_check.sh`

`EXIT=0`, 13 of 13, on the host, driving `pipeline.repo_deps.install` and
`agent.sandbox.run_contained`:

```
python install    STATUS installed  3.4s   volume comrade-deps-1192a48cbad748b6
node install      STATUS installed  1.2s
venv python       SIX_OK /deps/venv
the repo's tests  EXIT 0
Node ESM import   ESM_IMPORT_RESOLVED           <- F46, on the real host
run phase cannot reach metadata / arbitrary / platform_db / host_gateway
both installs went through the proxy
leftover volumes after the run: 0
```

🔴 **Four of my own mistakes in that script, every one found by running it.**
`Recipe.name`, not `.manifest` — my getattr defaulted to None and printed
"RECIPE None" about a recipe that had matched. `install()` returns "installed" or
"current"; I asserted "ok" and called a successful install a failure.
`recipe_for` returns the FIRST match, so one tree with both manifests installs
Python and never touches npm — the Node ESM check was running against a volume
nothing Node had touched. And the cleanup failed into `/dev/null` because the app
image prints a warning on import and I took all of stdout as a volume name, so
the next run found the volumes still there, correctly reported "current", made no
network request, and failed two checks about a run where the product was right.

### 5. The gates and the live usefulness checks

```
scripts/gates.sh   GATE EXIT: 0
  backend    1713 passed, 8 skipped, 21 deselected  (exclusive DB)
  frontend   229 passed / 34 files; integration 21 passed / 6 files
  browser journeys   8 passed
  real GitHub        2 skipped — no credential configured

pytest tests/test_agent_usefulness_live.py -m live   4 passed, ONE ask each
  task lookup   11.0s   wiki 9.3s   team context 6.8s   consent: card staged
  empty model calls during that run: 0
```

### 6. Backup, rollback, and two things that made the backup impossible

Taken from production with Comrade's own tooling and **verified by restoring it**
into a throwaway server on the host:

```
globals.sql    9,254 bytes   (role password hashes)
database.sql 750,442 bytes   40.4s
sha256 recorded in latest.json

restored ->  comrade roles 5   policies 112   public tables 35
             migrations 66     teams 2       rls-enabled tables 33
```

Rollback images tagged `:rollback-5c73fd0` for api, agent-worker,
pipeline-worker and frontend.

🔴 **The backup tool could not run in production at all** (`ee9a9a0`, `30df774`).
The app image had no `pg_dump`, so `scripts/backup.py` fell through to its
container fallback — which F37 makes it correctly refuse, because the database is
a remote Supabase instance and a local container would be a different server. And
`scripts/` was not copied into the image either. So `docs/deployment.md`'s
"rehearse restoration" could only ever run on a developer's laptop against a
developer's database. Found by trying to take a backup.

🔴 Restoring `globals.sql` into a cluster that already has a `postgres` role
fails under ON_ERROR_STOP (`role "postgres" already exists`), and the Supabase
platform roles cannot be granted on a plain server. The five comrade roles come
back regardless, which is what the policies need — but the documented
"restore the globals first" step needs a note, and it does not have one yet.

### 7. After deployment

```
public HTTPS      GET /               200   tls_verify=0
                  GET /api/health     200   {"status":"ok","database":"ok"}
                  certificate         Let's Encrypt, CN=13-62-11-26.nip.io,
                                      valid 2026-09-05 .. 2026-12-04
                  http://             308 -> https://
sign-in page      <title>Comrade</title>, #root, /assets/index-*.js 200, 558,868 B
auth boundary     GET /api/threads/<uuid>/agent-runs  no token    401
                  same with a bogus token                         401
                  POST /api/agent/turn no token                   401
readiness         GET /api/ready 200 — database, roles, migrations, agent_queue,
                  pipeline_queue, expired_leases, workers, sandbox: all ok
document ingestion  parsed by the running pipeline worker, status=ready,
                    the text came through
agent answers       INTERMITTENT — see below
teardown            the designated test team removed, rows left: 0
```

🔴 **NOT a browser sign-in.** Nothing here typed a password or created an account.
Sign-in is verified as far as the page rendering, its bundle being served, and the
API refusing unauthenticated calls. A real signed-in session on the deployed host
is NOT verified by me.

### The one thing that is not right: empty model responses

The deployed stack answers, and not reliably. On the same key, same model, within
one day:

```
local, 21:35   4 of 4 judge prompts answered, one ask each, 0 empty calls
host,  21:42   2 of 3 answered   (54.1s, 39.7s; empties recovered by the retry)
host,  21:50   1 of 3 answered   (two prompts exhausted all six attempts)
measured rate  0% .. 45% .. 64-76% of CALLS empty, in different windows
```

What it is NOT, each ruled out with a measurement:

  * not the key — the container's key is `sha256[:12]=480139766ec2, len 39`,
    the same one that answered 4/4 locally. My first reading said "different
    key, len 40"; that was my own shell capturing the `\r` from a CRLF line in
    `.env`, and the container receives 39 characters with no CR;
  * not quota — a minimal direct call answered 3/3 while agent turns were failing;
  * not the tool surface — a bare ADK runner with all 26 tools answered 8/8;
  * not thread history — a fresh thread answered 10/10;
  * not tool use — a tool-requiring prompt on a fresh thread answered 5/5;
  * not the experimental JSON_SCHEMA_FOR_FUNC_DECL path — disabling it made
    **every** call empty, 36/36, so it is load-bearing;
  * not request pacing — 8 seconds between turns left the rate unchanged.

`EMPTY_TURN_ATTEMPTS = 6` is the mitigation and it demonstrably works — several
deployed turns recovered on attempts 2-6. At a 70% per-call rate six attempts
still leaves roughly one turn in eight silent, and the member gets the error
notice instead of an answer.

🔴 I cannot fix this in code, and raising the constant again would be treating a
number I have already recalibrated once. It is an upstream, time-varying
condition on the configured Gemini key/model, and it is the reason **agent
usefulness at one ask is not a guarantee this deployment can make right now**,
even though it held in the 21:35 window.

### What the deployment is, plainly

Structurally deployed and verified: HTTPS, certificate, readiness, the auth
boundary, document ingestion, dependency installation through an enforced egress
policy, repository command execution with the sandbox unable to reach the
metadata endpoint, a platform database port, the host gateway or an arbitrary
destination, both workers on the daemon, a verified backup and tagged rollback
images.

Not verified: a browser sign-in on the deployed host, and reliable agent answers
at one ask under the model's current behaviour.


## Twelfth review — deployed `460f161`, ledger `5b04cc4`

**Deployment confirmed; F52 remains a material judge-experience problem, and
setup isolation has a newly reproduced hole.**

Independent live SSM checks confirmed `460f16173e90ba048feff933b833710042f81cab`
on EC2, six services running, both worker containers reaching Docker 29.1.3,
and public HTTPS `/api/ready` reporting all eight checks OK. Focused local
registry/deployment tests: **41 passed in 37.11s**. This review did not rerun the
full gate, authenticate a production browser, repeat live-model prompts, or
restore the production backup. Those boundaries distinguish independent
verification from the implementation ledger's evidence.

### F54 — Dependency setup can reach services on the Docker host

- [ ] **P1 — sandbox network boundary.** `agent/sandbox.py:327–353`
  (`_internal_network`), called by `run_setup`; acceptance gaps in
  `scripts/proxy_egress_check.sh` and `scripts/repo_execution_check.sh:129–130`.
- `_internal_network` creates an ordinary `docker network create --internal`
  bridge and attaches the registry proxy. Internal bridge mode blocks external
  routing, but retains the host bridge address. A dependency hook can connect
  directly to services bound to that address or all host interfaces, without
  asking Squid. The Squid hostname allowlist cannot block traffic bypassing it.
- **Reproduced on the actual pilot host:** created a disposable `--internal`
  network, attached the existing registry proxy as setup does, and ran the
  deployed sandbox image with read-only filesystem, all capabilities dropped,
  no-new-privileges, and resource limits. A plain TCP connection from that
  container to its actual bridge address **172.20.0.1:22 succeeded**:
  `HOST_SSH_TCP_REACHABLE`. No authentication was attempted and no application
  data, credentials, or metadata were read. This proves host-service reachability,
  NOT SSH authentication or host compromise. The client exited and the temporary
  proxy attachment/network were removed successfully.
- Evidence: SSM command `fa311dce-6325-49fb-b018-40fa8e5b238c` returned Success.
  Docker's documented distinction is explicit:
  https://docs.docker.com/engine/network/port-publishing/#gateway-modes
  (`internal` versus gateway mode `isolated`).
- Existing acceptance missed this: the proxy check's direct probe only tries
  public PyPI, while the four escape probes are in the **run** phase, which uses
  `--network none`. Its `172.17.0.1` is not the per-setup bridge address, and
  `127.0.0.1:54322` inside a sandbox is that sandbox's own loopback, not the
  deployed hosted database. Refusals there do not prove setup-to-host isolation.
- **Required fix:** create setup bridges without host-address reachability,
  e.g. supported `isolated` gateway mode for the enabled address families, or
  equivalent narrowly scoped enforced filtering. Keep registry-proxy transport
  working. Verify the resulting topology, not only `.Internal=true`. Apply the
  shared network helper's contract consistently to every caller; do not enable
  unsafe previews as part of the repair.
- **Acceptance:** exercise real `run_setup` with a known reachable host listener
  as a positive control, then prove the setup container cannot connect to it
  directly (IPv4 and IPv6 where enabled). Test actual platform endpoints and
  metadata, arbitrary direct egress, and forbidden proxy requests separately.
  A locked dependency install through the proxy must still pass. Clean up all
  test resources and propagate cleanup errors. This can be repaired without
  rolling back unrelated application improvements.

### F55 — Post-deploy acceptance can succeed despite failed cleanup

- [ ] **P2 — acceptance reliability / fixture lifecycle.**
  `scripts/post_deploy_check.py:52–78,198–215`; related shell cleanup in
  `scripts/proxy_egress_check.sh` and `scripts/repo_execution_check.sh`.
- `teardown()` catches delete failures, prints them, and never marks the run
  failed. The final check counts only the team row; leftover profiles/auth users
  can therefore coexist with `POST-DEPLOY OK` and exit 0. `furnish()` also runs
  before the `try/finally`, so a partially failed fixture is not cleaned up.
- **Reproduced without a database:** injected a connection that refuses profile
  deletion but reports the team gone; `main()` printed `POST-DEPLOY OK` and
  returned **0**. Injected a partial `furnish()` failure; observed initial
  teardown, partial fixture creation, then exception with no final teardown.
- **Required fix:** protect fixture creation with the same `try/finally` as the
  checks. Aggregate/raise cleanup failures and make them affect process exit.
  Verify all owned fixture roots (including auth user/profile), not just the
  team. The shell acceptance scripts should likewise fail if their owned
  network/container/volume cleanup fails, rather than swallow or merely print it.
- **Acceptance:** inject a mid-fixture failure and a teardown deletion failure;
  prove cleanup is attempted and a nonzero outcome is preserved. An ordinary
  passing run must leave no owned fixture rows/resources. Do not add broad
  cleanup patterns or remove real-team data.

### F52 remains open — upstream attribution is not established

The deployment ledger reports 2/3 then 1/3 useful host answers, sometimes taking
roughly 40–54 seconds. This is still a primary demo failure despite a successful
infrastructure release. Raising retries is mitigation, not diagnosis.

The claim that this is necessarily upstream and cannot be fixed in code is not
supported by those experiments. Successful bare calls, a bare tool-equipped
runner, and fresh-thread turns leave Comrade's full assembled request, replayed
history, session state, plugins, and response/event conversion as live hypotheses.
In particular, fresh-thread success does not rule OUT a history-dependent defect.
Comparisons must control the request and timing, not just the key/model name.

**Next fix method:** on a designated test team, capture safe structural provider
response diagnostics (candidate/part counts, finish/block reason, usage, errors)
and corresponding ADK events before Comrade filters them. Compare the same
failing request through the direct provider call, minimal ADK, and full runtime,
then change one request/history/plugin factor at a time. Keep private content and
credentials out of logs. Stop labelling the cause upstream until that comparison
supports it; do not raise retries again as the only fix. Require one-user-ask
usefulness and latency measurements on the deployed request path.

### Recovery evidence caveat — F36 fresh-cluster acceptance remains incomplete

The deployment ledger itself records globals restoration failing on the existing
`postgres` role and platform-role grants. `scripts/backup.py:510–512` still sends
that globals file to `psql` with `ON_ERROR_STOP=1`; its printed manual commands at
`:564–565` omit that flag and bypass the preparation contract. A manual partial
or adapted restore is not proof that the supported fresh-cluster restore path
works. Preserve the useful backup artifacts, document/implement exact role and
platform provisioning, and rerun the supported recovery procedure on a fresh
disposable target with failure propagation. No new destructive restore was run
by this review.

**Disposition:** keep the functioning application deployment; prioritize F54 and
F52. Close F55 and the explicit recovery-procedure gap without calling local
counts or readiness proof of a useful authenticated judge journey. Browser
sign-in on production remains unverified.


## F52 implementation — 2026-09-10 (local verification)

The empty response reaches Comrade directly from Gemini: a normal STOP candidate
with no parts and no output/thinking tokens. The earlier conclusion that this
could not be improved in application configuration was unsupported.

Controlled comparisons used the same synthetic task question, full system prompt,
22 tools and Gemini 2.5 Flash. Three initial baseline calls were empty; a short
replacement instruction and a one-tool configuration each answered 3/3. Merging
user messages and changing "silent teammate" did not reliably fix it. Neither
change ships. Appending an instruction to answer also still failed.

Thinking-budget comparison: explicit 1024 answered 9/9 raw calls. In the final
interleaved six-sample comparison, dynamic (-1) was empty 4/6, 4096 was empty 2/6,
and 1024 was empty 0/6. This isolates a usable provider configuration, not the
provider's internal reason for its empty STOP response. No claim of a permanent
zero-error rate follows from this sample.

**Change:** `agent/agent.py` sets `thinking_budget=1024`, keeping reasoning enabled,
all tools and the full safety prompt. Existing bounded empty-turn retries remain;
no model switch, new dependency, prompt weakening or extra retry was added.
The budget limits reasoning headroom for harder repository tasks; those need
separate acceptance before claiming equivalent complex-task quality.

**Regression:** the four live usefulness tests now disable runtime retries and
permit exactly one ask with no environment override. Before the change, task,
wiki and team-context tests failed with empty replies (3 failed, 1 passed). After
it, the four scenarios passed in three consecutive runs (12/12), including real
tool results and consent staging without execution. A local configuration guard
also fails before the fix. Focused agent/empty-turn tests: 15 passed.

Full gate and deployment status are recorded below when measured. This entry does
not close F54/F55 or claim production acceptance. Temporary provider request
captures and diagnostic tests were removed.

Provider documentation: [Gemini thinking budgets](https://ai.google.dev/gemini-api/docs/generate-content/thinking?hl=en)
documents dynamic (-1), disabled (0), and explicit budgets for 2.5 Flash. It does
not document the root cause of this empty-output behavior.

Additional live coverage: wiki-fact retrieval passed. The legacy history test
failed before a model call because `_resolve_thread` no longer accepts a thread
type or returns a scope object. Updated that test to resolve its seeded private
thread through the current helper; removed its second-ask allowance and disabled
runtime retries so recall is also measured at one ask. Result pending below.

**Full gate:** `scripts/gates.sh` exited **0**, captured from the process itself
(no output pipeline): backend **1716 passed, 8 skipped, 21 deselected**; frontend
build/lint passed; unit **229/34 files**; integration **21/6 files**; browser
**8 passed**. Real GitHub **2 skipped** (credentials unavailable). The realtime
integration's first subscription trial timed out and its built-in retry passed;
this remains a known flake, not a first-trial pass. Gate log:
`.codex-f52-gates.log`. No production deployment performed for this change.

**History acceptance:** repaired live recall test passed (1/1), with runtime
retries disabled and no second ask. F52 is implemented and verified locally;
production acceptance remains pending.


## Twelfth review follow-up — 2026-09-10, F52 root cause, F54, F55

### F52 — the cause was ours, and my "upstream" claim was wrong

🔴 **RETRACTED.** I wrote that the empty responses were "an upstream,
time-varying condition on the configured Gemini key/model" that "I cannot fix in
code". The reviewer said that was unsupported by my experiments, and they were
right. Successful bare calls, a bare tool-equipped runner and fresh-thread turns
rule out some hypotheses; they do not establish that the assembled request is
innocent. I had stopped at the point where a cause was inconvenient.

The cause is configuration and it is ours: `gemini-2.5-flash` runs with DYNAMIC
thinking by default, and on ordinary lookups it returned a normal STOP candidate
with no parts. Interleaved six-sample comparison on the same question, full
prompt, all tools:

```
thinking_budget -1 (dynamic)   empty 4/6
thinking_budget 4096           empty 2/6
thinking_budget 1024           empty 0/6      and 9/9 on raw calls
```

`agent/agent.py` now sets `thinking_budget=1024`, keeping reasoning enabled with
every tool and the whole safety prompt (`cabc70b`).

Measured after it, locally, with the runtime's empty-turn retries DISABLED so
nothing masks a first-attempt failure:

```
task lookup    4.0s   1 ask   answered
team context   4.3s   1 ask   answered
wiki question  4/5 over five runs, 3.5-5.2s
```

Latency fell with it: the deployed browser session before this change took 25-54
seconds per answer, and these take three to five.

🔴 **A DIFFERENT GAP REMAINS, and it is not the empty turn.** The one wiki
failure in five answered from chat history — "The chat history does not show a
decision about the team mascot" — instead of reading the wiki page that holds it.
The turn produced a reply, so the empty-turn retry does not apply and would not
have helped. That is tool selection, roughly 80% on this prompt at one ask, and
it is a usefulness gap I am naming rather than rounding up. I have no clean
before-measurement to say whether the thinking budget changed it.

### F54 — `--internal` is not isolation · `c3508b4`

`_internal_network` created the per-run setup network with
`docker network create --internal`, and the docstring asserted that this "gives
it no gateway". It does not. An internal bridge blocks EXTERNAL routing and
still has a gateway address on the host, so a dependency hook — pip's setup.py,
npm's postinstall, running as root with the network on — can open a TCP
connection to whatever the host has bound there. Squid is not in that path.

Reproduced on the pilot host with a controlled listener rather than SSH, and
with a positive control proving the listener was genuinely up
(`scripts/setup_isolation_check.sh`):

```
docker network create --internal        .Internal=true   gateway=172.20.0.1
a sandbox container on that network     REACHED the host listener
...with gateway_mode_ipv4=isolated      BLOCKED (OSError)
...and the registry proxy on it         still OK 200
```

🔴 **My own escape probes could not have caught this, and I read them as if they
could.** They run in the RUN phase, which uses `--network none`, and they aim at
`172.17.0.1` — the DEFAULT bridge, not the per-run one. Refusals there say
nothing about the setup phase's network. I reported "all four escapes blocked"
as though it covered both phases.

The fix removes the gateway on both address families, and READS THE TOPOLOGY
BACK: `.Internal` was true on the network that reached the host, so the flag
proves nothing by itself. Any gateway is refused, which also covers a network
left over from a release predating this. The gateway list is parsed as JSON
rather than through a Go template, because `{{.Gateway}}` prints the literal
string `invalid IP` when empty and a future Docker spelling it differently would
silently flip the answer. An unparseable IPAM configuration is an error, not an
allow.

Mutation-checked: removing the isolated options and the read-back fails 3 tests.

### F55 — a failed cleanup reported success · `e2aae4e`

Three defects in one lifecycle. `teardown()` caught each delete failure, printed
it and returned None, so a run whose cleanup failed still exited 0. The final
check counted the team row only, so a leftover profile or auth user could coexist
with success. And `furnish()` ran BEFORE the `try/finally`, so a fixture that
failed halfway left its rows with no teardown at all.

All three fixed, and proved by injection without a database, the way the finding
reproduced them:

```
a teardown deletion fails             -> exit 1, not POST-DEPLOY OK
a fixture root survives the teardown  -> exit 1
furnish fails halfway                 -> teardown still ran
an ordinary clean run                 -> exit 0
```

`auth.identities` is in the teardown now. A browser sign-in needs one and the
first version did not know it existed — which is exactly how the previous
fixture left one behind.

### The live fixture that was left in production, and removing it

The `cabc70b` browser verification created a real team, user, profile, identity,
wiki page, task, thread, six messages, four agent runs and two consent rows in
the production database, and left them there. Removed by id, with every root
verified afterwards:

```
removed  12 agent_steps   2 consent_queue   4 agent_runs   1 memory_versions
          1 memory_entries 1 memory_pages   1 tasks        6 messages
          1 threads        1 memberships    1 teams        1 profiles
          1 auth.identities 1 auth.users
LEFTOVER  {team 0, profile 0, auth_user 0, identity 0, runs 0, consent 0, tasks 0}
REAL_TEAMS_REMAINING  2  MLOps, MealShare
```

### What that verification did prove, before it was cleaned up

A real browser sign-in on the deployed host — the thing I had explicitly not
done — with password authentication through the live form, then:

```
"Who is on this team?"                  -> "The team has one member: F52 Checker."
"What did we decide the deployment
  mascot would be?"                     -> "The deployment mascot is a quokka.
                                            This decision is recorded in the
                                            'Deployment decisions' wiki page."
"Please propose a task ... assigned
  to me."                               -> consent card, AWAITING YOUR APPROVAL
before approval                         -> tasks 0, consent [pending task_create]
after Approve once                      -> card COMPLETED, task created 'proposed'
worker log for that team                -> every request got a response;
                                           no empty turns at all
```

The consent gate held: zero tasks existed while the card was pending, and the
task appeared only after approval.

🔴 One of those four runs later ended `failed` after 79s, and a follow-up
question left a second run parked at `waiting_for_permission` with an unanswered
card. Both were cleaned up with the fixture rather than diagnosed.

### Still open

🔴 **The Supabase admin API key is rejected.** Creating the test user through
`/auth/v1/admin/users` returned `401 Invalid API key` with the configured
`SUPABASE_SECRET_KEY`, under both the `apikey`-only and `Authorization: Bearer`
shapes. The fixture was created by direct database insert instead. Nothing in the
product uses that endpoint today, but `shared/storage.py` sends the same key to
Storage — so either the key is wrong for the admin API only, or document
downloads are running on borrowed time. Not diagnosed here.

🔴 Tool-selection reliability on the wiki prompt, above.

🔴 F36's fresh-cluster restore: `scripts/backup.py` sends the globals file to
`psql` with `ON_ERROR_STOP=1` while its own printed instructions omit the flag,
and restoring globals into a cluster that already has `postgres` fails. The five
comrade roles do come back. The supported path has still not been run on a fresh
target.

### Deployed and verified on the host

```
master e2aae4e, six containers up, api healthy

the DEPLOYED _internal_network makes:
    .Internal = true      gateways = NONE
    options   = {"com.docker.network.bridge.gateway_mode_ipv4":"isolated",
                 "com.docker.network.bridge.gateway_mode_ipv6":"isolated"}

and a real install through it, in the deployed agent-worker:
    INSTALL installed 3.6s     RUN exit 0
    proxy log: TCP_TUNNEL/200 pypi.org, files.pythonhosted.org
    no leftover setup networks, dependency volume removed

scripts/gates.sh   GATE EXIT: 0
    backend 1720 passed, 8 skipped, 21 deselected  (exclusive DB)
    frontend 229 / 34 files; integration 21 / 6; browser journeys 8
    real GitHub 2 skipped - no credential configured
```


### F56 — the release migrates through an image it never builds

Found by running the post-deploy acceptance the way its own docstring says to,
and getting `No module named scripts.post_deploy_check` out of a container that
had been built two deploys earlier.

`migrate` is gated behind a Compose profile, and `docker compose build` builds
only the default profile. So `deploy_host.sh` step 4 never touched it:

```
compose config --services            api agent-worker pipeline-worker
                                     frontend caddy registry-proxy
  --profile migrate config --services ...and migrate

comrade-api        built 2026-09-10 11:15   (e2aae4e)
comrade-migrate    built 2026-09-09 21:27   (30df774, two deploys behind)
  missing from it: post_deploy_check.py, setup_isolation_check.sh
```

🔴 **The check script is not the consequence that matters.** Step 5 applies
migrations with `$COMPOSE run --rm --no-deps -T migrate`. A release that adds a
migration runs the OLD migrator, which does not contain the new file, and
reports success having applied nothing — new code against an unmigrated schema,
with the deploy green. The migration counts happened to match here (106 = 106)
only because the last two releases were Python-only. That is luck, not a
control.

Fixed with `--profile "*"` rather than the name, so a service gated behind some
later profile is built on the day it is added. Verified on the host: the build
then reports `migrate Built` and the image carries the whole `scripts/`.

🔴 **My first version of the real-compose test passed against the live defect.**
`compose config` without a profile omits gated services from the model
entirely, so it compared the same six services to themselves. It enumerates
from `--profile "*" config` now and asserts `migrate` is in that set.

### Post-deploy acceptance, on the deployed stack

The designated test team, created and removed by the run itself — the first
time this has been executed against production rather than reasoned about:

```
POST-DEPLOY OK   exit 0   116s

agent answers: task lookup    PASS  28.7s  'There is one open task: "Verify the
                                            telemetry exporter", ass...'
agent answers: wiki question  PASS  25.3s  'The team decided the deployment
                                            mascot is a quokka.'
agent answers: team context   PASS  25.0s  'The team has one member: Checker.'
document ingestion: parsed by the running worker   PASS  status=ready
document ingestion: the text came through          PASS  'pomegranate...'
teardown: every fixture row is gone                PASS  left: none

real teams before / after: MealShare, MLOps  (untouched)
fixture team rows 0, fixture auth users 0
```

Three prompts, three useful answers, **one ask each**, no error notices. The
wiki question is the one I flagged at roughly 80% — it answered from the wiki
here rather than from chat history.

🔴 **Latency is 25-29s on the host against 3.5-5.2s locally, and I have not
established why.** Each question here pays a cold start: the check runs the
agent in-process in a fresh one-off container, and the ADK client is
constructed twice per answer. That is a plausible explanation and it is not a
measurement, so it stays open rather than being written off.

🔴 **Skipped lane: the agent WORKER path.** These three answers were produced
in-process and created no `agent_runs` rows, so the queue, the lease, and the
empty-turn retry did not run. Worth stating precisely rather than implying
coverage: the last time that lane ran was the `cabc70b` browser session (four
runs, every request answered, no empty turns), and `git diff cabc70b..HEAD`
touches only `agent/sandbox.py` — `agent/agent.py`, `agent/runtime.py`,
`server/`, `shared/` and `pipeline/` are unchanged. The answer path deployed
now is the one that was exercised there.

🔴 **The empty-turn rate on this build is unmeasured, not zero.** Grepping the
worker for retry lines returned 0 — out of 0 turns, because the worker restarted
at 11:16 and nothing has queued a run since. `agent_runs` holds four rows all
time and none in the last 24 hours.

### No judge traffic during any of this

`select status, count(*) from agent_runs where created_at > now() - interval
'24 hours'` returns nothing at all, so the container recreation these releases
performed interrupted no one.

### F57 — the gate could not be run correctly in one invocation

Found by running it. The browser lane needs an API on :8000 and told the
operator to start one; the backend lane needs the database to itself. Doing
what the message said produced a failure in a lane that had nothing to do with
it.

Same commit, two runs an hour apart:

```
API down   1722 passed, 8 skipped        browser lane EXITED 1 without running
                                         "the API on :8000 is not answering"
API up     1721 passed, 1 FAILED         browser lane never reached
           test_agent_history::test_stream_turn_seeds_the_session_with_the_thread_history
           AssertionError: at index 0 'A1 private note' != 'earlier question'
           - the thread came back missing a message the test had just written

that same test, alone, with the API still up:   3/3 passed
```

So it is contention, not the API's existence. `seeded` is function-scoped and
cleans up either side, the suite has no random ordering and no xdist, and the
insert timestamps cannot reorder — the row was simply not there when the
session was seeded.

🔴 **The failing assertion is not root-caused and I am not claiming it is.**
What is established is the trigger (a second writer) and that it is unrelated
to F56: the diff touches `scripts/deploy_host.sh` and its test only, and the
run with no competing writer passed all 1722. The underlying question — why a
concurrent writer makes history seeding drop a committed row — stays open.

Fixed in the harness so the trap cannot be set again:

- the backend lane REFUSES to start while anything answers on :8000, naming the
  reason, instead of flaking thirteen minutes later somewhere unrelated;
- the browser lane starts its own API afterwards and stops it on the way out,
  so one invocation satisfies both.

Verified: with an API up the gate now exits 1 in under a second with that
message.

I got the second half wrong first. I made the browser lane start its own API,
which broke it two ways: `wait` on a killed process returns non-zero and `set
-e` took that as the gate failing, so a run in which every lane passed — 1722
backend, 229 frontend, 21 integration, and the 8 browser journeys that had
never run before — still exited 1 after the last of them. And it was redundant:
`frontend/playwright.config.ts` already starts the API, the agent worker and
the dev server as its own `webServer` entries. The lane was self-sufficient the
whole time; the precondition check only ever talked the operator into putting a
second writer on the database for the lane above. Removed, net 40 lines shorter
than the version I first committed.

`reuseExistingServer: true` is why the old check existed at all — a pool opened
before a `--with-reset` holds handles to a dropped database, and Playwright
would adopt it. The backend guard covers that better: nothing can be answering
on :8000 by the time this lane runs, so Playwright always starts a fresh one.

### F58 — the release could skip migration and activation and report success

The most serious thing found today, and it was found only because F56 made me
look at what a deploy actually did rather than at whether it went green.

The workflow ran the release as `git show $SHA:scripts/deploy_host.sh | sh -s
$SHA`, so the script is its own stdin. `docker compose run` reads stdin, and can
therefore swallow **the rest of the script**: `sh` reaches end of input, exits
0, and the deploy reports success having never migrated, never activated and
never checked readiness.

Measured on the pilot host, twice:

```
the 0d933af deploy          6 seconds, workflow green
  output stopped dead at step 4b's "Valid configuration"
  all four containers still on the PREVIOUS images afterwards
  the running api did not contain the scripts/ of the commit it had "deployed"
  api log lines during the deploy window: 0   (step 7 never ran)

re-run by hand, exactly as the workflow invokes it:
  323 lines, last line "Valid configuration", REDEPLOY EXIT: 0
```

🔴 **It is a race**, between how much `sh` has buffered and when the one-off
container grabs the pipe. That is why some releases activated and some did not,
and why no single passing run can demonstrate a fix. A probe of the same shape
truncated on one run and completed on the next.

So the fix is structural rather than empirical: **the workflow writes the script
to a file and runs it**, and there is then no shared stdin for anything to
drain. The exit code is preserved across the cleanup, so a failed deploy cannot
report success that way either. Belt and braces, every one-off container in the
release also redirects from `/dev/null`.

Verified by the next deploy, which is the only proof that counts here:

```
deployed dca9019           79 seconds, against 19 for the broken one
api / agent-worker / pipeline-worker / frontend    SAME (activated)
created 14:09:33-34, inside the deploy window 14:09:10-14:10:29
the running api carries this commit's scripts/     4 redirects present
readiness ran                                      1 api log line, was 0
/api/ready all eight ok    /  200    POST /api/agent/turn  401
```

#### Four wrong hypotheses, and the pattern in them

🔴 I blamed the missing `-T` flag on the caddy validate step. **Adding `-T`
changes nothing** — measured, both forms truncate. The flag controls the TTY,
not whether stdin is attached.

🔴 My first probe used `sudo docker compose`; the release does not. 🔴 My second
used a four-line script, small enough that `sh` had already read all of it
before the container started. Neither could contain the defect. It reproduced
the moment the probe was 155 lines, like the real thing.

🔴 **And the regression test I first wrote had no teeth.** It ran the release
past a stdin-draining docker double and asserted it still migrated — but piped,
on this machine, it still migrated, so the test passed on the broken shape. I
deleted it rather than keep it as reassurance. What replaced it is the part that
is checkable anywhere: the workflow must not pipe the release into `sh`, and no
`$COMPOSE run` may inherit its stdin. Both mutation-checked — restore the pipe
and one fails, drop a redirect and the other does.

That is the same failure as F56's first test and as the F54 escape probes: a
fixture that does not contain the thing it is guarding. Four times in one day,
which is why it is now written down in the memory rather than just here.

#### What this means for the earlier entries in this ledger

🔴 **Every "deployed and verified" line above this one is weaker than it reads.**
A green deploy did not establish that the release ran to completion, so which of
those releases actually activated is unknown. What is still sound is anything
checked directly against the running system afterwards — the isolation and
install checks, the post-deploy acceptance, the public HTTPS and auth-boundary
probes — because those interrogate the deployed stack rather than trusting the
release. The gap was between `up -d` and the badge, and everything measured on
the far side of it stands.

### Post-deploy acceptance on the build that actually activated

Re-run against `dca9019`, because until F58 was fixed the provenance of what
was running could not be relied on:

```
POST-DEPLOY OK   exit 0
  task lookup    28.6s  PASS
  wiki question  25.4s  PASS
  team context   32.6s  PASS   <- after "empty turn from the model (attempt 1/6)"
  document ingestion: parsed by the running worker    PASS  status=ready
  teardown: every fixture row is gone                 PASS  left: none
```

That empty turn replaces an "unmeasured" note earlier in this document with a
figure: **empty turns still occur on this build**, one in three answers here,
and `EMPTY_TURN_ATTEMPTS` absorbed it on the first retry. The answer still
arrived within one user ask, which is the property that was asked for — but
"the thinking budget fixed the empty turns" would be the wrong reading. It
reduced them; the retry is still load-bearing.

🔴 **The canonical gate has NOT been run against `dca9019`.** It went green on
`ed7f527` (GATE EXIT 0, all lanes, browser journeys included) and the commits
after that touch `.github/workflows/deploy-pilot.yml`, `scripts/deploy_host.sh`,
its test file, and documentation — nothing any product image imports. What ran
instead is that file's own tests, with both new ones mutation-checked. The full
gate could not run because local Docker lost every container mid-session and the
Supabase stack it needs had not come back; `npx supabase start` was still
pulling when this was written. Stated rather than papered over.

### Release preconditions, before pushing

```
rollback images for the build that was running
  comrade-{api,agent-worker,pipeline-worker,frontend}:rollback-e2aae4e
  (rollback-460f161 and rollback-5c73fd0 still present)

backup, taken through the migrate image
  20260910T125849Z-1e7f9239/  database.sql 864K, globals.sql 9.1K

VERIFIED BY RESTORING IT, not by existing:
  restored in 215.3s                       exit 0
  tables=43  policies=128  rls_tables=41   migrations=106
  teams=2  messages=10  threads=5  pages=33
  restored copy: MealShare, MLOps          comrade roles in globals.sql: 5
  scratch left behind: 0                   LIVE teams untouched
```

Four of my own mistakes on the way there, none of them product defects:
`python scripts/backup.py` instead of the documented `python -m scripts.backup`
(the path form puts `scripts/` on `sys.path` and `shared` disappears); `--out`
instead of a positional directory; a bind mount owned by root while the image
runs as uid 10001; and a raw `psql -f` that failed on `schema "public" already
exists` because it skipped `prepare_target`, which is the whole point of the
supported path.

One of them is worth keeping as a note rather than a mistake: I wrote
`... | tail` and read `$?` as the backup's status. It is `tail`'s. That is the
same trap as the earlier `gates.sh | tail -1` retraction, so the status is
captured before any pipe now.

🔴 **The backup sits on the same host and the same disk as the database it
came from.** Verified restorable, but it is not off-host, so it does not cover
losing the instance or the volume. Naming the limit rather than letting
"verified backup" imply more than it is.

### The gate took five runs, and each failure was a different real thing

Recorded in full rather than as the green one at the end.

```
run 1  backend 1722 pass    browser lane refused: no API on :8000
                            (I had stopped it earlier in the session)
run 2  backend 1721 / 1 F   the API left up during the backend lane -> F57
run 3  ALL LANES PASS       exit 1 anyway, from the teardown I had just added
       1722 / 229 / 21 / browser 8 passed (36.8s)
run 4  backend 1721 / 1 F   google 503 UNAVAILABLE in an unmarked test
run 5  ALL GREEN         GATE EXIT: 0, "all gates passed"
```

🔴 **`tests/test_remember_this.py::test_the_compilation_records_that_a_human_asked`
calls the live model and carries no marker.** `pyproject.toml` deselects
`live`, `realgithub` and `scenario`, and this test has none of them, so the
default backend lane depends on Google being up: a 503 there fails the whole
canonical gate. It passed on re-run in 12.19s. Left as a finding rather than
fixed — marking it `live` removes real coverage from the default lane and
stubbing the call is a test-strategy decision, neither of which belongs in a
deployment change.

Run 3 is the one worth keeping for what it proved, since it was the first time
every lane ran together on this commit: the browser journeys pass 8/8,
including the consent journey and a real agent turn against `gemini-2.5-flash`
— two 200s, no empty turns — driven through an agent worker that Playwright
started itself.

### Gate

```
[1m=== frontend lint ===[0m
[1m=== frontend unit + component ===[0m
 Test Files  34 passed (34)
      Tests  229 passed (229)
[1m=== frontend integration ===[0m
[realtime] first trial failed (no realtime event within 15000ms — is the table published, the realtime container up, and its replication stream settled?); retrying once in case the replication stream was restarting.
 Test Files  6 passed (6)
      Tests  21 passed (21)
[1m=== browser journeys (playwright) ===[0m
  8 passed (44.8s)
[1m=== real GitHub end to end ===[0m
============================== warnings summary ===============================
2 skipped, 1749 deselected, 5 warnings in 9.41s
[1;32mall gates passed[0m
```



## Thirteenth review — 2026-09-16, HEAD `50ba2ec` plus uncommitted F59

Independent checks: public `/api/ready` returns 200/all eight checks OK. SSM
confirmed host checkout `50ba2ec` and API configuration `thinking_budget=1024`.
Read-only probes using the configured secret still return **Auth 401 / Storage
400**. No credentials or user records were printed. No deployment or production
fixture mutation was performed in this review.

Focused command: `pytest tests/test_supabase_key_check.py
tests/test_deploy_host_script.py tests/test_registry_proxy.py -q`:
**44 passed, 9 failed**. Eight failures are missing test helpers; one is the local
Docker engine being unavailable. A full gate was not attempted against that
unavailable stack. Existing uncommitted app/invite/storage changes were preserved.

### F59 — configuration remains broken; local diagnostics are not a repair

The uncommitted changes in `server/app.py`, `server/invites.py`,
`shared/storage.py` and `tests/test_supabase_key_check.py` report rejected keys;
they do not supply a valid key. Invites and Storage-backed document ingestion
remain affected. The deployed readiness endpoint has no `supabase_api` field.
The proposed check is advisory, so even after shipping it HTTP 200 remains
possible with those features broken.

Required: configure a valid server-side key for the actual Supabase project,
verify both Auth and Storage from their consuming services, and run a real
upload/download/parse journey (inline `payload.content` bypasses Storage).
Then verify a controlled invitation with an explicitly authorized recipient.
Do not describe diagnostic-only changes as restoring the features.
The new Storage comment promising automatic recovery is too strong: the worker
stops retrying after `MAX_ATTEMPTS = 3`. After fixing configuration, failed
parse jobs need an explicit safe requeue/recovery procedure.

### F60 — P1: F58 repair still converts a failed deployment into success

Location: `.github/workflows/deploy-pilot.yml:56`, test at
`tests/test_deploy_host_script.py:241`.

The double-quoted AWS `--parameters` argument contains unescaped `rc=$?` and
`exit $rc`. GitHub's local Bash expands both before sending the command to SSM.
Executing the exact argument through Bash with a harmless AWS stand-in produced:
`... sh /tmp/comrade-deploy.sh deadbeef; rc=0; rm -f /tmp/comrade-deploy.sh; exit `.
A remote release exiting 7 followed by that tail reports 0. The current test only
asserts the variable spellings are present in the YAML, so it passes this defect.

Required: preserve remote variable evaluation (escape the dollar signs correctly,
or construct the command as data with proper JSON encoding). Keep the script-file
fix for stdin. Add a test evaluating the actual runner command construction and
executing its resulting remote wrapper against a release that exits 7; require 7
through cleanup, and 0 for success. Do not redeploy through an unverified wrapper.

### F61 — P1: deployment regression tests no longer run

`dca9019` deleted `_fake_host`, `_double`, and `_as_the_workflow_does` while tests
still call them (`tests/test_deploy_host_script.py:292,310,330,366,376,400,605,621`).
Eight tests now fail with NameError before reaching any release behavior. These
are not Docker availability failures. The separately reported Docker failure is
at line 557. The three selected F58 tests hid the broken remainder of the file.

Required: restore/adapt the shared fixture helpers to file-based invocation,
including the PATH guard; run the entire deployment test file, then the canonical
gate when local Docker/Supabase are available. Preserve the negative stage checks.

### F62 — P2: isolation-check cleanup still returns success and is overbroad

`scripts/setup_isolation_check.sh:32-45,167`: `exit "$FAILED"` evaluates its code
before the EXIT trap. Setting `FAILED=1` inside cleanup cannot change that exit.
A local shell reproduction using the actual cleanup function with a failing
Docker-remove stand-in printed `could not remove ...` and exited **0**.
The function enumerates every `comrade-isolation-check-*` network and removes
shared `/tmp/comrade-f54-*.py` paths, instead of resources owned by this invocation.
Concurrent checks can destroy each other's fixtures.

Required: explicitly preserve/merge the main and cleanup statuses and exit with
the merged code from the trap (avoid recursion), and clean only this invocation's
network names and unique temporary directory. Test failed cleanup yields nonzero
and another invocation's resources are untouched. This reopens the shell portion
of F55; the Python teardown repair does not fix this script.

### F63 — P2: `--with-agent-eval` still cannot own its required API lifecycle

`scripts/gates.sh:108-114` refuses an existing API. Playwright starts and stops
its API/agent worker within the browser lane, but the later optional agent-eval
lane (`:180-186`) requires that API to already be running. Nothing starts it there;
`--quick --with-agent-eval` also has no API startup path. This is established by
control-flow inspection, not an executed live scenario.

Required: start/own the API and agent worker around the scenario lane after the
DB-exclusive tests, clean them on both success and failure, and keep the backend
precondition. Prove one invocation reaches and completes the opted-in scenario.

### Priority and remaining limits

1. Repair F60/F61 before another release; restore the valid Supabase key and prove
   actual document ingestion/invitations rather than merely exposing diagnostics.
2. Run one complete browser -> API -> worker -> consent approval -> resumed answer
   journey. The prior browser run's 79s failure and re-parked consent were removed
   with the fixture without diagnosis; in-process acceptance does not close them.
3. Measure and reduce live 25-33s latency. Earlier worker logs showed model calls
   around one second with multi-second gaps around them; cold one-off containers
   alone do not explain the already-observed long-lived-worker latency.
4. F52 remains a measured mitigation, not a zero-empty guarantee: the latest
   deployed acceptance itself needed a retry. Wiki tool selection also remains
   imperfect. Keep one-ask usefulness checks with retries visible.
5. Complete F62/F63 and the remaining recovery/retention/preview/Box acceptance
   backlog. F54's isolated-network configuration and F56's profile-inclusive build
   are useful repairs; this review found no new defect in their core changes.


## Answer to the thirteenth review — 2026-09-16

F60, F61, F62 and F63 are repaired. F59 is repaired only where it is a code
defect; the key itself is not something this branch can supply, and that is
said plainly below rather than dressed up.

### F60 — the deploy workflow reported success for failed releases. FIXED

Reproduced first, with the workflow's own `--parameters` argument run through
bash against an `aws` stand-in that prints what it is handed:

```
commands=["cd /opt/comrade && ... && sh /tmp/comrade-deploy.sh deadbeef; rc=0; rm -f /tmp/comrade-deploy.sh; exit "]
```

`rc=0` is the RUNNER's `$?`, `$rc` expanded to nothing, and a bare `exit` exits
with the status of the preceding command — the `rm`, which always succeeds. So
a release exiting 7 reached SSM as a success. Every deploy since `dca9019` has
been unable to report failure.

`.github/workflows/deploy-pilot.yml`: `\$?` and `\$rc`, escaped, so both
evaluate on the host. `$GITHUB_SHA` deliberately still expands on the runner.
Verified through the same stand-in: the payload now carries `rc=$?` and
`exit $rc`, and the wrapper returns 7 for a release exiting 7, 0 for 0.

`tests/test_deploy_host_script.py::test_a_failed_release_cannot_report_success`
replaces the assertion that passed on the defect (`"rc=$?" in workflow` — true
of the broken file too). It BUILDS the command by executing the workflow's own
send-command line, then RUNS the wrapper that comes out of it against a release
that exits 7. Mutation-checked twice:

* remove the escaping → fails, printing the exact broken payload above;
* keep both spellings but capture the status after the `rm` instead of before
  → fails with `a release exiting 7 was reported as 0 through the cleanup`.

The second mutation is why the test cuts the wrapper at the release invocation
rather than at `; rc=`: anchoring on `rc=` follows the mutation and passes.

### F61 — eight deployment tests did not run. FIXED

`dca9019` inlined `_fake_host` into the parametrised test and deleted
`_fake_host`, `_double` and `_as_the_workflow_does` while eight tests still
called them; they failed with NameError before reaching any release behaviour,
and the three tests that commit ran by `-k` could not show it.

The three helpers are restored, `_as_the_workflow_does` now running the release
from a FILE — the shape F58 moved to — and the parametrised test uses them
instead of its own copy, so there is one fixture rather than two that can drift
apart again. `tests/test_deploy_host_script.py`: **31 passed**, including the
six failure stages and the F60 test above.

### F62 — the isolation check hid cleanup failures and removed other runs'
resources. FIXED

Both halves, in `scripts/setup_isolation_check.sh`:

* the EXIT trap now decides the status. `exit "$FAILED"` at the bottom
  evaluated FAILED *before* the trap ran, so `FAILED=1` set inside cleanup went
  into a variable nothing read again;
* cleanup removes only `$CUR` and `$FIX` — this invocation's two networks —
  instead of enumerating every `comrade-isolation-check-*` on the host, and the
  probe programs moved from fixed `/tmp/comrade-f54-*.py` names into a
  `mktemp -d` directory removed with the run.

`tests/test_setup_isolation_check.py`, five tests against a `docker` double
that reports a network belonging to another invocation as present. Run against
the pre-fix script, four of the five fail, and on the two that matter the
failure is the defect itself:

```
AssertionError: the cleanup reported a failure and the check still exited 0
  ... *** could not remove comrade-isolation-check-99999-current

AssertionError: the check enumerated other invocations' networks
  ... 'network disconnect -f comrade-isolation-check-99999-current comrade-registry-proxy',
      'network rm comrade-isolation-check-99999-current'
```

The fifth (a real FAIL in the body must stay nonzero through a clean cleanup)
passes both before and after, which is what a control is for.

### F63 — `--with-agent-eval` could not reach its own scenario. FIXED

The lane demanded an API on :8000 that no invocation of the script can leave
there: the backend precondition exits 1 while anything answers on :8000,
Playwright stops the one the browser lane uses, and `--quick --with-agent-eval`
skips that lane entirely. The opted-in scenario was unreachable by
construction, which is why no run ever reported it failing.

`scripts/gates.sh` starts and owns the API around the scenario, beside the
pipeline worker it already owned, stops both on every exit path through the
trap, and keeps the backend precondition untouched. It also warns if `:8000` is
still answering afterwards — `uv run` is a parent process, and an orphan there
makes the NEXT run refuse to start at a check that points nowhere near this
lane.

`tests/test_gates_agent_eval_lane.py` runs the real script end to end with `uv`,
`npm`, `curl`, `docker` and `powershell.exe` doubled, starting from the state
the script's own precondition requires — nothing answering on :8000. The API's
health answer exists only while the uvicorn double is alive, so the scenario
double fails if the lane did not start one. `--quick --with-agent-eval` reaches
`sim.scenario --check` and completes; the same test against the pre-fix script
stops at `agent eval (deterministic scorer tests)` and never reaches it.

This is the control flow proven by execution. It is NOT a live scenario run:
that needs the real model, a real GitHub repository and a valid Supabase key.

### F59 — the key. NOT FIXED, and it cannot be fixed from here

`SUPABASE_SECRET_KEY` is a credential for the pilot's Supabase project. Nothing
in this branch can mint one, and nothing here should: it has to be issued in
the project's own dashboard and set on the host. Auth 401 / Storage 400 stand,
invites and Storage-backed ingestion stay broken, and the readiness check that
ships with this work REPORTS that rather than repairing it. The review is right
that diagnostics are not a repair, and no claim is made that they are.

What was a code defect here has been fixed: `shared/storage.py` said the failed
parse jobs "should recover by itself once a valid key is configured". They do
not. The worker parks a job as `failed` after `MAX_ATTEMPTS = 3`, which takes
minutes — the jobs from the broken window are already parked long before anyone
notices the key. The comment now says so and carries the requeue, checked
against the schema (`last_error`, `available_at`, `finished_at`, `worker_id`,
and `attempts = 0`, without which the claim query skips the row forever).

Two defects in the F59 work itself, found by pointing it at a HEALTHY stack —
which is the thing a check has to be tried against before anyone trusts it.
Both are the same mistake: a status code treated as a diagnosis.

* `_supabase_check` reported every status >= 400 as a rejected key. The local
  GoTrue answers `500 Database error finding user` to `/auth/v1/admin/users`
  with a perfectly valid key, so a healthy deployment read as a credential
  failure — and the remedy it named, issue a new secret key, is not the
  remedy. Each plane now carries its own rejection statuses (auth 401/403,
  storage 400/401/403, as measured on the pilot), and anything else reports
  `the service is unwell (this one is not about the key)`.
* `download_document`'s guard listed 401/403 — and the measured storage
  rejection is a **400**. It did not fire on the shape the review reported.
  400 alone cannot be the rule either: the local stack answers 400 with
  `Object not found` for a document that is genuinely missing. The body is the
  discriminator, and both directions are pinned.

Three more the F59 change had not finished, found by running the whole suite
rather than a selection — which is how F61 got in:

* `shared/storage.py` read `response.status_code` off a streamed response, and
  the two stand-ins in `tests/test_object_authenticity.py` did not have one.
  Nine tests failed with AttributeError. The doubles now carry the attribute a
  real `httpx.Response` has.
* `/ready` reports a ninth check, and `tests/test_readiness.py` pinned the set
  of names it may report.
* `docs/operations.md` had no `supabase_api` entry, which that file's own test
  requires of every check the route reports. It now documents the advisory
  policy, both planes, and the requeue.

### Not done, and why

* **A real upload → download → parse journey, and a controlled invitation.**
  Both go through the rejected key. They are the first things to run once it is
  valid, and neither is evidence until then. The local stack's key IS valid, so
  the code path is exercised locally by the suite; that is not the pilot.
* **The PAUSED RUN resuming after an approval, end to end in a browser.** The
  browser lane's consent journey does pass — approve the T2 card, the tool
  executes, the task appears — but `frontend/tests/e2e/global-setup.ts` seeds
  that `consent_queue` row with **no `agent_run_id`**, so nothing in that
  journey goes through `_requeue_permission_run`. The resume is covered
  in-process instead (`tests/test_permission_continuation.py`, six tests
  including `the_resumed_run_is_the_same_logical_run`, plus
  `tests/test_agent_resume.py`), against the real database. The review's point
  stands as stated: in-process acceptance is not the browser journey, and this
  round did not close that gap.
* **The 25–33s live latency on the pilot.** Measured here instead, which is a
  different machine and therefore not an answer: the browser lane's live turn
  took **6.9s** end to end, two real `gemini-2.5-flash` calls inside it (1.8s
  and 3.2s, both 200), and no empty response. One sample, locally. It says the
  long pole is not an unconditional in-process stall on this code path; it says
  nothing about the host, which is where the 25–33s was measured.

### Gate

The local Docker engine is back, so the backend suite ran in full against it —
the whole file every time, not a selection:

```
uv run pytest -q
  before  11 failed, 1726 passed, 8 skipped   (9 storage doubles, 2 readiness)
  after   1740 passed, 8 skipped, 21 deselected, in 642s

tests/test_deploy_host_script.py           31 passed
tests/test_setup_isolation_check.py         5 passed  (4 fail on the old script)
tests/test_gates_agent_eval_lane.py         3 passed  (1 fails on the old script)
tests/test_supabase_key_check.py           11 passed

cd frontend && npm run test:e2e             8 passed (37.5s)
  journey 5  inline consent: approving the T2 item executes it
  journey 8  live agent turn, 2x gemini-2.5-flash 200, no empty reply, 6.9s
```

`scripts/setup_isolation_check.sh` was also run against the REAL daemon rather
than only its doubles. Docker Desktop puts the internal network's gateway
inside the VM, so the positive control cannot bind it and the check stops there
— which is the interesting case: it exited **1**, `docker network ls` was
byte-identical before and after, and no `/tmp/comrade-f54-*.py` was left. On the
old script that same run would have exited 0.

Not run: the real-GitHub lane, and `--with-agent-eval` itself. The agent-eval
repair is proven by executing `scripts/gates.sh` with doubles, which establishes
that the lane is reachable and completes — not that the live scenario passes.


## Fourteenth independent review — 2026-09-16

Reviewed the uncommitted answer to F59–F63 on HEAD `50ba2ec`.
No application repairs or deployment performed by this review.

Independent verification:

```
python -m pytest tests/test_deploy_host_script.py tests/test_setup_isolation_check.py tests/test_gates_agent_eval_lane.py tests/test_supabase_key_check.py -q -p no:cacheprovider
50 passed in 49.05s
```

F60 and F61 pass the focused deployment suite, including execution of the
workflow-produced remote wrapper. F62's original network ownership and exit
status defects pass its five regression tests. These are local checks; they
are not production deployment acceptance. The full gate was not independently
rerun in this review. The repairs remain uncommitted.

### F63 — REOPENED, P2: the evaluation lane still lacks its execution worker

`scripts/gates.sh:200–204` starts uvicorn and `pipeline.worker`, but never
`agent.worker`. `server/app.py:667–672` only enqueues turns; the pipeline worker
does not consume that queue. Playwright owns its own agent worker and stops it
with the browser lane; `--quick --with-agent-eval` does not run that lane at all.
A fresh invocation therefore has nobody to execute the scenario's agent turns.
The scenario double in `tests/test_gates_agent_eval_lane.py:59–64` checks only
an API marker and succeeds without an agent worker, so its green result does
not prove the real lane works.

Required fix: own API, pipeline worker AND agent worker for the scenario, wait
for their readiness, and stop/wait for the actual child processes on success,
failure and interruption. Use existing worker readiness/lifecycle patterns.
Do not kill unrelated processes. Lines 222–235 currently kill only `uv` parents
and explicitly allow an API left behind to produce `all gates passed` after a
warning; the failure trap does not even check for that leak. Test a process
that actually spawns a child, and require cleanup of all owned children rather
than a double whose parent removes its own marker. Require a scenario failure
to remain nonzero through cleanup. A fresh second invocation must be possible
without manual process cleanup.

### F64 — P2: the scenario still speaks the pre-durable-turn API

`sim/scenario.py:83–109` posts `{team_id, text, thread_type}`. The current
`TurnRequest` requires `thread_id`; executing the actual request model class
from `server/app.py` against that exact payload shape produces:

```
[(('thread_id',), 'missing')]
```

Thus the first ask is rejected with 422, even after fixing F63. Adding only
`thread_id` is insufficient: `TurnResponse` contains only `run_id` and `status`,
but `ask()` immediately reads `reply` and `user_message_id`, records an empty
answer, and moves on without waiting for the durable run. Its evidence then
cannot reliably assess completed answers or distinguish agent-input citations.
This is an existing scenario defect exposed by reviewing the newly repaired
lane, not a claim that this patch introduced the API mismatch.

Required fix: resolve/create the fixture's real group and private threads,
submit their IDs, then follow the supported durable run API until completion
or a permission/user wait, with bounded timeouts and explicit failed/cancelled
handling. Collect actual reply and input-message evidence from the persisted
run/messages. Handle consent waits explicitly where the scenario requires
execution; do not infer success from the admission response. Reuse the current
frontend/API-test protocol rather than inventing another transport. Add a
contract test using the real request/response models and a queued-then-completed
run; it must reject the old payload and must not record the queued response as
a finished answer. Finally run the real opted-in scenario; a shell double alone
cannot establish acceptance.

### Still blocking useful production features

F59 remains open: the previous live observation was Auth 401 / Storage 400;
this patch adds diagnosis and documentation, not a replacement credential.
This review did not re-probe production or change its configuration. Verify a
valid key on the host, then exercise a real Storage upload/download/parse and
controlled invitation. Requeue affected failed parse jobs deliberately after
repair. Post-approval continuation in a real browser and production answer
latency/reliability remain unverified by this patch.

Next priority: restore F59's live functionality first; finish F63/F64 and its
process cleanup before claiming the optional quality gate works. Then run the
canonical gate, deploy the reviewed repairs, and verify the actual affected
user journeys. The 50 passing focused checks are useful evidence, not a claim
that the system is fully repaired.


## Answer to the fourteenth review — 2026-09-16

F63 and F64 are repaired, and this time the lane was RUN rather than only
doubled — which is how the last F63 answer got away with being half a repair.
F59 is unchanged and still open; the reason has not changed either.

### F63 — the lane had no execution worker, and cleaned up only parents. FIXED

Both halves were real.

**The missing worker.** `POST /agent/turn` calls `enqueue_turn` and returns —
its own docstring says "agent.worker executes it" — and the pipeline worker
does not read that queue. A lane with an API and a pipeline worker admits every
turn in the scenario and executes none of them. `scripts/gates.sh` starts all
three now.

**The cleanup.** `kill` on a `uv run` reaches the WRAPPER; the python it spawned
keeps :8000. Rather than chase children, the lane resolves the interpreter once

    PY="$(uv run python -c 'import sys; print(sys.executable)')"

and starts each server with it, so the pid the script holds is the pid that
serves. `lane_stop` then kills all three, WAITS for them to actually exit (a
delivered signal is not an exited process), and runs from a trap on EXIT, INT
and TERM. If anything is still answering on :8000 afterwards the gate now
FAILS: the old code printed a warning and let `all gates passed` follow it,
which is a warning nobody reads and a refusal the next run meets at a check
pointing nowhere near this lane.

Readiness is `/ready`'s own worker check rather than a sleep. It reports ok
only once both an agent and a pipeline worker have beaten recently, and it
cannot answer at all unless the API is up and reaching the database — so one
poll covers all three processes.

`tests/test_gates_agent_eval_lane.py` was rewritten around what the review
asked for: the doubles are processes that record their own pids, "the API
answers on :8000" is `kill -0` on the pid that serves, and the leak case is
modelled the way it actually happened — the pid the script holds is a parent,
and the thing holding the port outlives its kill. Eight tests. Mutation-checked
against the previous, half-repaired script:

* remove the agent worker → **7 of 8 fail** (only the no-flag control passes);
* warn instead of failing on a leaked server → the leak test fails, printing
  `all gates passed` beside `WARNING: something is still answering on :8000`.

### F64 — the scenario spoke the pre-durable-turn API. FIXED

Worse than reported, and the extra part would have stopped it before the first
ask: `say()` inserted `messages (team_id, thread_type, ...)`, and `thread_type`
was dropped from that table when `thread_id` became not-null. The scenario was
speaking to a schema and an API that had both moved on.

* **Threads.** `_thread_for` resolves the team's General thread (created by
  trigger, one per team, so it is SELECTED) and, for a private ask, a
  restricted thread owned by the asker — created under that member's own
  session, through `au_threads_insert` and `au_thread_participants_insert`.
* **The durable run.** `ask()` posts `{team_id, text, thread_id}` to
  `/agent/turn/stream` and FOLLOWS the frames the browser follows: `run` and
  `status` for the lifecycle, `text` steps for the answer, `done` for a
  terminal run, with a bounded per-turn deadline and explicit failed/cancelled
  handling.
* **Approvals.** A turn that parks on a consent card is approved as the member
  who asked and then REATTACHED from the cursor already seen
  (`/agent/runs/{id}/stream?after_seq=`), up to four times per turn. A scenario
  that needs three tasks created and a pull request proposed cannot get them by
  admitting a turn and walking away — so this is the post-approval continuation
  path as well as the answer path.
* **Evidence.** `agent_input_message_ids` comes from `agent_runs.input_message_id`
  now. It used to be read off the admission response, which has no such field,
  so the list was always empty and every citation check silently passed.

`tests/test_scenario_turn_contract.py` — seven tests, using the real
`TurnRequest`/`TurnResponse`. The old payload is refused with
`(('thread_id',), 'missing')`; `TurnResponse.model_fields` is exactly
`{run_id, status}`, which is why waiting is not optional; a stream that ends
`queued` raises rather than recording an answer; the consent path asserts the
approve call, its body and the `after_seq` on the reattach. Mutation-checked:
send the old payload → the transport test fails; never approve → the consent
test fails with the run still `waiting_for_permission`.

#### What only a live run could find

🔴 `_thread_for` used `insert ... returning id` for the private thread, and
that fails against the real database: `au_threads_select` is
`can_access_thread`, which for a restricted thread means a `thread_participants`
row — and the creator has none at the moment of the INSERT. So `RETURNING`
fails the SELECT policy on the row it has just written, and there is no way to
look the id up afterwards either. The id is generated client-side now, which
breaks the deadlock without weakening anything: `is_thread_creator` is security
definer, so a creator can add themselves to a thread they cannot yet read.

Worth recording as a product gap rather than a scenario one: there is no
thread-creation endpoint, and whoever writes it will meet this on the first
restricted thread.

### The lane, run for real

Not a double: `scripts/gates.sh`'s own process lifecycle, executed around one
live turn against the local stack. No repository and no pull request — see
below.

```
interpreter: D:\OneDrive\Desktop\Comrade\.venv\Scripts\python.exe
started api=2020 pipeline=2021 agent=2022
READY: all three reporting
  Tom Alvarez    → Comrade: I joined late - what did the team decide about
                  rendering, and who is on this team?
  Comrade        The team decided on a plain text grid, no curses, 20x20 for
                 rendering, as Aisha Khan stated on 2026-09-16. The team
                 members are Priya Sharma, Aisha Khan, Marcus Lee, Tom Alvarez.
                 (12s, run 30614899, done)
runs        : [{'input_tokens': 20628, 'output_tokens': 111,
                'seconds': 11.61, 'status': 'done'}]
input msgs  : ['80b57b57-d0cb-45b1-8472-418855f41f36']
http errors : []
LIVE TURN OK
PORT FREE after cleanup
ALL THREE STOPPED
```

Both repairs in one run: three processes started and reported ready, a thread
row resolved, a real turn followed to `done` with a grounded answer, an
`input_message_id` persisted where the list used to be empty, and every process
stopped with the port free. The first attempt at this is what found the
`RETURNING` defect above.

### Not done, and why

* **The full opted-in scenario (`gates.sh --with-agent-eval`).** It clones
  `Ranveersingh1113/test`, runs generated code and OPENS A REAL PULL REQUEST on
  it. The credentials for that are configured locally, so it would work — which
  is exactly why it is not run unasked. Everything up to the repository is
  proven above; the GitHub half is one confirmation away.
* **F59.** Unchanged. `SUPABASE_SECRET_KEY` is a credential for the pilot's
  Supabase project; nothing in this branch can mint one, and the readiness
  check that ships here REPORTS the rejection rather than repairing it. Auth
  401 / Storage 400 stand, and no claim is made otherwise.
* **Production latency and the browser-journey resume.** Unchanged from the
  previous round, and the previous round's limits still read correctly.

### Gate

```
uv run pytest -q
  run 1   1750 passed, 9 skipped, 1 failed   in 699s
  run 2   1752 passed, 8 skipped, 0 failed   in 863s

tests/test_deploy_host_script.py           31 passed
tests/test_setup_isolation_check.py         5 passed
tests/test_gates_agent_eval_lane.py         8 passed   (7 fail without the agent worker)
tests/test_scenario_turn_contract.py        7 passed
tests/test_supabase_key_check.py           11 passed
```

Both runs are reported, because run 1's single failure is worth naming rather
than rounding off: `tests/test_remember_this.py::test_the_compilation_records_that_a_human_asked`,
which passed on its own immediately afterwards (14.88s) and passed again in run
2. It is the flake this ledger already recorded two reviews ago — it calls the
live model and carries no `live` marker, so the default lane depends on Google
being up — and it is still not fixed, because marking it `live` removes real
coverage and stubbing the call is a test-strategy decision.


## Fifteenth independent review — 2026-09-16

Reviewed the uncommitted F63/F64 follow-up. The missing agent worker, real
thread IDs for chat/turns, durable stream following, consent reattachment and
persisted input-message evidence are implemented. This review did not run the
external PR-producing scenario, change production, or independently rerun the
full backend suite. Two completion claims still exceed the implementation.

### F63 — still open, P2: surviving workers are forgotten with success

`scripts/gates.sh:60–75`: after 30 polls, `lane_stop` clears `lane_pids` and
only echoes surviving PIDs. That echo returns zero. The later port check tests
only the API, so a pipeline/agent worker that survives TERM can continue
mutating the database while the gate prints `all gates passed`. This is not
just a hypothetical signal handler: both real workers deliberately drain the
current job after their first TERM, and model/compiler work can exceed 30s.
Clearing the PID list also prevents the EXIT trap from trying again.

Independent reproduction executed the actual `lane_stop` function against an
owned child ignoring TERM; only the polling sleep was accelerated:

```
surviving_worker=yes cleanup_exit=0 tracked_pids=
still running after kill: 28
```

The review forcibly stopped its own test child afterwards. Existing tests use
workers that exit immediately on TERM; the orphan test covers only the API.

Required fix: preserve owned PIDs through cleanup, give them a bounded graceful
drain, then stop surviving owned processes and reap them with `wait`. Propagate
cleanup failure if termination cannot be established; preserve an earlier
scenario failure. Cover a worker that survives the first signal with the API
already stopped. Give INT/TERM explicit nonzero exits after cleanup: currently
`trap lane_stop EXIT INT TERM` runs cleanup but does not itself require the
interrupted gate to terminate. No broad process-name kills.

### F64 — still open, P2: the promised whole-turn timeout is not enforced

`sim/scenario.py:184–215,235–251,255–291`: the deadline is tested only between
permission-wait segments. `_consume` never checks it. `httpx.Timeout(900)` is
an inactivity timeout on reads, not a 900-second limit for an entire streamed
response. The real API sends a heartbeat every 15 seconds, so a queued/stuck
run can keep the initial or resumed stream open indefinitely without ever
reaching the deadline condition. Each resumed stream also receives the full
original timeout rather than the remaining budget.

Independent reproduction invoked actual `ask`/`_consume` with a 0.01s budget
and a delayed heartbeat followed by a completed answer after 0.062s:

```
DEADLINE REPRO: done elapsed 0.062 limit 0.01
```

It returned success past the deadline. This used a transport double, without
model calls or database writes; the missing elapsed-time check is in the real
consumer, not httpx.

Required fix: enforce one monotonic deadline throughout initial streaming,
approval requests and reattachment, including heartbeat-only streams. Bound
blocking reads by the remaining budget and reject frames/completion received
after expiry. Record an explicit timeout failure and deliberately handle any
still-running owned run rather than claiming it finished. Test heartbeats
crossing the deadline and a resumed segment exhausting the original budget.
Keep the successful grounded-turn evidence, but do not treat it as proof of
bounded failure handling or post-approval acceptance.

F59 remains open as reported. Production upload/invitation recovery remains
higher user-impact work than these evaluation-harness repairs. The real full
scenario and deployed browser continuation remain acceptance gaps; a single
local read-only answer does not cover them.

Independent focused verification: `test_deploy_host_script.py`,
`test_setup_isolation_check.py`, `test_gates_agent_eval_lane.py`,
`test_scenario_turn_contract.py`, and `test_supabase_key_check.py`: **62 passed
in 69.75s**. The two reproductions above expose cases those green tests omit.


## Answer to the fifteenth review — 2026-09-16

Both reproductions were right, and both are fixed. F59 is unchanged.

### F63 — surviving workers were forgotten with success. FIXED

`lane_stop` polled for 30 seconds, then **cleared** `lane_pids`, echoed the
survivors and returned **zero**. Three consequences, all real:

* a pipeline or agent worker that outlived TERM kept writing to the database
  while the gate printed `all gates passed` — and the port check cannot see
  that, because a worker holds no port;
* clearing the list meant the EXIT trap had nothing left to try again with;
* the drain window is not generous. Both workers deliberately finish the job in
  hand on their first TERM, and model or compiler work outlasts 30 seconds.

Now: TERM, a bounded drain, then `kill -9` on the **owned pids that remain**,
then `wait` to reap them, then a final check — and if termination still cannot
be established, `lane_stop` returns nonzero **with the pids kept**. `lane_exit`
merges that verdict with the gate's own status, so a cleanup failure fails the
run and a scenario that already failed keeps its own code. Nothing matches on
process names; only pids this lane started are ever signalled.

`LANE_DRAIN_SECONDS` exists so a test can exercise the escalation without
waiting half a minute for it. Two tests added. The survivor case is the review's
shape exactly — the API stops normally, the pipeline worker ignores TERM — and
against the previous `lane_stop` it fails with the defect printed verbatim:

```
AssertionError: still running after kill: 1701
  ... stderr='still running after kill: 1701', stdout=... 'all gates passed'
```

#### One claim withdrawn

The interrupt test does **not** prove what `lane_interrupted` fixes. On this
host, signalling an MSYS bash tears down its process tree whatever the trap
says, so the assertions pass against the old `trap lane_stop INT TERM` as well —
checked, not assumed. The test is renamed to what it actually establishes (an
interrupted gate ends nonzero with nothing of ours still running) and says so in
its own docstring. `lane_interrupted` stays, because on a POSIX host the trap
runs the cleanup and then RESUMES the script, which is the case the review
named; I could not write a test on this machine that distinguishes it, and a
test that passes on the broken shape is worse than no test.

### F64 — the whole-turn timeout was not enforced. FIXED

`httpx.Timeout(900)` is an inactivity timeout on reads, not a cap on a streamed
response, and `_run_frames` emits a heartbeat every 15 seconds of silence —
which resets it forever. The deadline was consulted only between permission
waits, so a queued or stuck run could hold a stream open indefinitely, and the
review's reproduction returned success past the limit (`done elapsed 0.062
limit 0.01`).

* One monotonic deadline for the whole turn. `_consume` checks it **before
  folding each frame**, so a `done` that arrives past the budget is not recorded
  as a completed answer — checking after folding would have left exactly the
  reported behaviour.
* Every segment goes through `_stream_segment`, which bounds the request by
  `_remaining(deadline)`. Each reattach used to receive the full original
  timeout, so a turn with four approvals could legitimately run four times its
  stated limit. The approval POST is bounded the same way.
* A stream that goes completely quiet raises `httpx.TimeoutException`, which is
  caught as the turn running out rather than as the network breaking.
* On expiry the turn is recorded as an explicit failure **and the run it owns is
  cancelled**, not abandoned: when the budget goes the run is usually still
  executing, and walking away leaves a worker writing into the team that the
  next ask is about to read.

Three tests added, mutation-checked:

* delete the in-loop clock check → the heartbeat test and the resumed-budget
  test both fail (the `done` past the deadline is recorded as an answer again);
* give each segment a fresh full budget → the resumed-budget test fails.

### The lane, run for real again

The deadline work touched the live path, so it was re-run rather than assumed:

```
started api=2110 pipeline=2111 agent=2112
READY: all three reporting
  Comrade  The team decided on a plain text grid, no curses, 20x20 ... (9s, done)
runs        : [{'input_tokens': 12701, 'output_tokens': 67, 'seconds': 5.72,
                'status': 'done'}]
input msgs  : ['4f2a5f69-3cf1-4d05-bc2b-64b0c1275d19']
http errors : []
LIVE TURN OK / PORT FREE after cleanup / ALL THREE STOPPED
```

Still one local read-only answer, and still not the acceptance the review is
asking for. It is not offered as one.

### Unchanged, and in the order the review put them

1. **F59.** The pilot's `SUPABASE_SECRET_KEY` is rejected by its own project;
   uploads and invitations stay broken until a valid key is set on the host.
   Nothing in this branch can mint one. That is the higher-impact work and it is
   not work this branch can do.
2. **The real full scenario**, which clones a repository and opens a pull
   request on it. Not run, and not run unasked.
3. **The deployed browser continuation** and production latency.

### Gate

```
uv run pytest -q                            1757 passed, 8 skipped, 0 failed

tests/test_gates_agent_eval_lane.py          10 passed
tests/test_scenario_turn_contract.py         10 passed
tests/test_deploy_host_script.py             31 passed
tests/test_setup_isolation_check.py           5 passed
tests/test_supabase_key_check.py             11 passed
```


## Sixteenth independent review — 2026-09-16

Reviewed the latest uncommitted F63/F64 follow-up. The prior surviving-worker
and heartbeat-over-deadline reproductions are addressed: owned workers now
receive escalation/reaping, and stream frames are checked against a monotonic
turn deadline before being accepted. No production changes or external
PR-producing scenario were run by this review.

### F64 residual — P2: approval timeouts bypass owned-run cleanup

`sim/scenario.py:340–343` performs the approval POST outside any HTTP exception
handler. Unlike `_stream_segment`, it never translates `httpx.TimeoutException`
into `state['timed_out']`; control unwinds before `_note_timeout` can cancel the
known run or record failure evidence. A consent response can be lost after the
server approved/requeued the run, leaving work executing despite the scenario
having exited. The later gate teardown is not a substitute for cancelling that
specific durable run, which can be recovered by another worker.

Independent reproduction used actual `ask`, a parked-stream stand-in carrying
a known run/card, and an approval POST raising `httpx.ReadTimeout`:

```
raised: ReadTimeout
requests: ['http://localhost:8000/consent/owned-card/approve']
failure evidence: []
```

No `/cancel` was attempted. No real approval, model request or DB write occurred.

Required fix: handle timeout/transport errors around the approval POST as well
as streams. Record the uncertain approval outcome; do not blindly retry the
side effect. On an abort, attempt cancellation of the known owned run through
the existing cancellation helper and retain any cancellation failure in the
reported evidence. Add a regression where approval is accepted server-side but
its response times out; require failure evidence and cancellation of the same
run, with no success claim. The same abort policy should cover a transport
failure after stream admission when the run ID is already known.

This is an evaluation-harness failure-path gap, not evidence of a new deployed
product regression. F59's invalid production credential remains the higher
user-impact unresolved issue. The full real scenario, deployed continuation
journey and production latency still need acceptance; the local read-only
success does not establish those. Do not reopen the now-fixed missing-worker,
old API contract or original heartbeat checks under this narrower finding.

Independent focused run: deployment, isolation cleanup, gate lifecycle,
scenario contract and Supabase key suites: **67 passed in 106.47s**, exit 0.
The approval-timeout reproduction above is an additional uncovered case.
The full backend gate was not independently rerun this review.


## Answer to the sixteenth review — 2026-09-16

The residual was real and is fixed. F59 is unchanged, and remains the item with
the most user impact.

### F64 residual — an approval timeout bypassed cancellation. FIXED

The approval POST was the one HTTP call in `ask()` outside a handler, so
`httpx.TimeoutException` unwound straight out of the function: no cancellation
of the run, and no evidence either — `http_errors` is written by
`_note_failure`, which was never reached. The review's reproduction is exact:

```
raised: ReadTimeout
requests: ['http://localhost:8000/consent/owned-card/approve']
failure evidence: []
```

And the lost answer is the dangerous shape rather than a harmless one:
`approve_consent` requeues the run inside the same call, so a response that
times out may well be one the server already acted on — card approved, turn
running again.

**One abort path now.** `_note_timeout` became `_abort_turn(person, text,
state, reason)`, and every way of giving up goes through it: the whole-turn
deadline, a transport failure on either stream, a non-200 approval, and the
timed-out approval. It records the reason as failure evidence and cancels the
run whenever `state["run_id"]` is known — which covers the review's last point,
a transport failure after admission, because the opening `run` frame has
already carried the id. A cancellation that itself fails is kept in the reason
rather than swallowed; "we could not stop it" is the part an operator needs.

The POST is **not** retried. Repeating it would be blindly repeating a side
effect; the uncertainty is recorded instead:

    approving <card> did not answer within 60s, so it is unknown whether the
    card was approved and the run requeued; cancel returned HTTP 200

Four tests added, all mutation-checked:

* put the approval POST back outside a handler → the two approval tests fail
  with a raw `ReadTimeout` and no evidence, exactly as reproduced;
* send a stream failure straight to `_note_failure` instead of the abort → the
  transport-after-admission test and the no-run-to-cancel control both fail.

The control matters as much as the rest: an abort before any run exists must
not invent one to cancel, and must still record the failure.

### The lane, run for real again

`ask()` changed, so the live path was re-run rather than assumed:

```
READY: all three reporting
  Comrade  The team decided on a plain text grid, no curses, 20x20 ... (7s, done)
runs        : [{'input_tokens': 13006, 'output_tokens': 68, 'seconds': 4.42,
                'status': 'done'}]
input msgs  : ['c161aa76-03fd-4b45-93af-9f215b5ba3bb']
http errors : []
LIVE TURN OK / PORT FREE after cleanup / ALL THREE STOPPED
```

Still one local read-only answer, still not the acceptance the ledger is
waiting on, and still not offered as one.

### Unchanged

1. **F59 — the pilot's rejected `SUPABASE_SECRET_KEY`.** Uploads and
   invitations stay broken until a valid key is set on the host. Nothing in
   this branch can mint one; what ships here reports the rejection rather than
   repairing it. It is the highest-impact open item and it is not work this
   branch can do.
2. **The real full scenario**, which clones a repository and opens a pull
   request on it. Not run unasked.
3. **The deployed continuation journey and production latency.**

### Gate

```
uv run pytest -q                            1761 passed, 8 skipped, 0 failed

tests/test_scenario_turn_contract.py         14 passed
tests/test_gates_agent_eval_lane.py          10 passed
tests/test_deploy_host_script.py             31 passed
tests/test_setup_isolation_check.py           5 passed
tests/test_supabase_key_check.py             11 passed
```


## Seventeenth independent review — 2026-09-16

Reviewed the unified abort-path follow-up on the uncommitted worktree at
`50ba2ec`. No new actionable defect found in this targeted repair.

Independent focused verification (deployment, isolation cleanup, gate
lifecycle, scenario contract, Supabase key checks): **71 passed in 105.39s**,
process exit 0. The full backend suite was not independently rerun.

Re-executed the previous approval-timeout reproduction against actual `ask`:
the approval POST times out, exactly one cancellation is attempted for the
same known run, and one error entry is retained in TRANSCRIPT. No approval
retry occurs. The former raw ReadTimeout/empty-evidence failure is fixed.
Stream transport and non-200 approval paths now also route through
`_abort_turn`, which retains the cancellation outcome in failure evidence.
The specific F64 residual from review sixteen is locally verified closed.

This does not establish deployment acceptance or all possible failure modes.
F59's production credential repair remains open. The full external scenario,
deployed browser approval/continuation, and production latency still require
verification. Changes remain uncommitted; this review performed no deployment,
production credential change, or external PR-producing scenario. Prioritize
restoring uploads/invitations rather than extending this harness review loop.


## F59 — the verification, built and proven — 2026-09-16

The credential itself is still the operator's to restore; nothing here changes
that, and the section below says exactly what that step is. What this round
does is make the two broken features CHECKABLE, because the acceptance script
that was supposed to catch F59 could not see it.

### The check was blind to the outage it was for

`scripts/post_deploy_check.py` handed the document's bytes to the job as
`payload.content`. That is the PREVIOUS shape — `handle_document_job` still
accepts it only so jobs queued by an older image are not stranded — and it
never touches the Supabase HTTP API at all. The product uploads to private
Storage and queues a reference, and the worker fetches the file back over the
very API that was answering 400. So post-deploy acceptance passed, on a
deployment where every real upload failed.

It now uploads the file, queues no content, and records the digest — which also
exercises the "the stored file changed after this document was queued"
comparison the handler makes. And an invitation check sits beside it.

### Proven by running it, both ways

Against the local stack, where the key IS valid:

```
=== document ingestion, through Storage and the pipeline worker ===
  document ingestion: the file reaches Storage         PASS
  document ingestion: parsed by the running worker     PASS  status=ready
  document ingestion: the text came through            PASS  'The fruit the team chose is a pomegranate...'
=== teardown ===
  every fixture row is gone                            PASS  left: none
POST-DEPLOY OK
```

And with the key replaced by a rejected one — the F59 shape exactly, the agent
answering fine while uploads fail:

```
  agent answers: task lookup                           PASS  3.9s
  agent answers: wiki question                         PASS  3.9s
  agent answers: team context                          PASS  6.2s
  document ingestion: the file reaches Storage         ***FAIL  HTTP 400
        {"statusCode":"403","error":"Unauthorized","message":"Invalid...
POST-DEPLOY FAILED: document ingestion: the file reaches Storage
```

The invitation half reports the named 503 the same way:

```
  invitation: the project accepted the invite   ***FAIL  503 the configured
        Supabase secret key was rejected by the project (401)...
```

#### What only running it could find

🔴 The first version uploaded with the service key and the worker REFUSED the
file: `this document does not name a file belonging to this team`.
`document_object_is_authentic` requires `storage.objects.owner_id` to equal the
document's `uploader_id` — a prefix check alone would pass a restricted
same-team attachment belonging to somebody else. In the product the member
uploads from their own session and the owner is theirs by construction; this
file has no browser sign-in, so the object is attributed to the uploader the
same way every other fixture row is inserted. A check written against an
assumed path would have shipped green and proved nothing.

### The invitation check is opt-in, and removes only what it created

An invitation creates an account and sends mail to a real person, so it runs
only when `COMRADE_CHECK_INVITE_EMAIL` names a recipient the operator has
authorised — never one this file picked, and skipped loudly otherwise.

The account is removed afterwards ONLY if this run created it. The address may
well belong to someone who already has one, and deleting that would be the
check damaging what it verifies. Both branches were exercised locally, against
GoTrue, with the account in branch 2 seeded independently of the check:

```
branch 1  no account beforehand -> created, invited, removed   (afterwards: None)
branch 2  seeded independently   -> invited, LEFT ALONE
          pre-existing 7a9e1392-f011-42e1-a3ab-a622f7a04f66
          afterwards   7a9e1392-f011-42e1-a3ab-a622f7a04f66
```

The uploaded object is removed by exact path in teardown as well — by id, never
by prefix, the same policy as every other fixture here — and a failure to
remove it is reported, because this file's cleanup already learned that lesson
(F55).

### What is still the operator's to do

Nothing in this repository can mint a Supabase secret key; it is issued in the
project's own dashboard. The order, once it is set on the pilot host:

1. Set `SUPABASE_SECRET_KEY` on the host and restart the stack.
2. `/api/ready` should report `supabase_api: ok`. If it reports
   `key rejected`, the key is still wrong; if it reports `the service is
   unwell (this one is not about the key)`, look at the project rather than
   the credential.
3. Run the post-deploy acceptance, which now proves the journey rather than
   assuming it:

   ```
   docker compose -f docker-compose.yml -f docker-compose.prod.yml \
     --profile migrate run --rm --no-deps -T --entrypoint python \
     migrate -m scripts.post_deploy_check
   ```

   Add `-e COMRADE_CHECK_INVITE_EMAIL=<an address you have authorised>` to
   include the invitation.
4. Requeue the parse jobs that failed during the broken window — the worker
   parks a job after three attempts and does NOT retry it once the key is
   valid. The statement is in `shared/storage.py`, beside the error it matches
   on.

### Gate

```
uv run pytest -q                            1761 passed, 8 skipped, 0 failed

scripts/post_deploy_check.py (local stack)  POST-DEPLOY OK
  ... with a rejected key                   POST-DEPLOY FAILED, on the upload
  ... invitation, both branches             exercised against GoTrue
```

The local Docker engine went down part-way through this round and was
restarted to run the above; the stack is the same one the backend suite uses.


## Eighteenth independent review — 2026-09-16

Reviewed the post-deployment Storage/invitation acceptance changes. The
Storage path now uploads bytes, queues a reference with the correct SHA-256
and no inline `content`, then waits for the running worker. A safe execution
of actual `document_is_ingested` with HTTP/DB doubles confirmed that payload
shape. This review did not send email, delete users, upload to production, or
independently repeat the live acceptance run. Adjacent key/scenario tests:
**25 passed in 4.76s**; these are not new invitation-cleanup regression tests.

### F65 — P1: invitation acceptance can delete a pre-existing account

`scripts/post_deploy_check.py:343–366` checks `auth.users where email=%s`, then
assumes `before is None` proves the subsequently returned user was created by
this invocation. But `_invite_or_resolve_user` resolves an existing user with
`public.user_id_by_email`, whose committed SQL explicitly compares
`lower(email) = lower(p_email)`
(`supabase/migrations/20260719150000_invites.sql:44–46`).

For an existing `existing@example.test`, an authorized recipient entered as
`Existing@Example.test` misses the case-sensitive precheck. The invite reports
already registered; the real helper resolves the existing ID; cleanup deletes
that user's identities and auth row and reports PASS. Exact-ID deletion does
not establish that this run owns the ID. Even a normalized precheck alone has
a race: a signup/invite between precheck and resolution is not this run's
account to delete.

Independent reproduction executed actual `an_invitation_is_delivered` AND
`_invite_or_resolve_user`, with a DB double modelling those two SQL comparisons
and an HTTP 422 already-registered response. It attempted:

```
delete from auth.identities where user_id=%s
delete from auth.users where id=%s
```

Both targeted the resolved pre-existing ID; the script printed both invite and
cleanup as PASS. All mutations were intercepted by doubles. No real account
or email was touched.

Required fix: never infer ownership from a missing precheck row. The simplest
safe choice for an arbitrary operator-authorized recipient is to preserve the
account and document that the invite check does not delete it. If automatic
account teardown is necessary, restrict it to a positively identified,
run-owned fixture with creation provenance; preserve existing/resolved users
and ambiguous outcomes. Normalize existence checks consistently, but do not
present normalization as solving the concurrent-creation case. Add cases for
mixed-case existing email and an account appearing between lookup and invite;
neither may issue identity/user deletes. Do not run the current opt-in invite
check against real users until this is repaired.

Acceptance ceiling: the upload uses a privileged key and manually attributes
object ownership. It now detects the F59 credential failure, but does not
prove browser upload RLS or browser sign-in. F59 itself remains unresolved;
these diagnostics do not restore production uploads/invitations.


## Answer to the eighteenth review — F65 — 2026-09-16

Right, and P1 is the right severity: the invitation check could delete a real
person's account. It deletes nothing now.

### F65 — ownership was inferred, and the inference was wrong

The pre-check asked

    select id from auth.users where email = %s

and "no row" was treated as proof that the id the invite returned afterwards
had been created by this run. Those are different claims, and the gap is
reachable two ways:

* **Case.** `public.user_id_by_email` — the resolver the invite actually uses —
  compares `lower(email) = lower(p_email)`
  (`supabase/migrations/20260719150000_invites.sql:44-46`), verified in the
  committed migration. So an authorised recipient typed `Existing@Example.test`
  against a stored `existing@example.test` missed the pre-check, the invite
  reported already-registered, the resolver returned the EXISTING id, and the
  cleanup deleted that person's identities and auth row — reporting PASS.
* **Time.** Normalising does not fix it, and the review is right to say so
  explicitly. A signup landing between the lookup and the resolution produces
  an account this run did not make.

Deleting by exact id does not establish ownership of that id, and for an
address the operator chose there is no provenance to appeal to. So the fix is
the one the review recommended: **preserve the account**. The check reports
which case it was and says, on its own line, that the account is left in place
and the id to remove if the invitation was not wanted.

The existence lookup is normalised as well — not as the safety mechanism, but
because asking a different question from the one the invite asks is how this
started, and reporting the wrong thing about somebody's account is its own
fault. The comment beside it says which of those two jobs it is doing.

`teardown()` still removes this file's own fixture user by its fixed synthetic
id. That one has provenance: `furnish()` creates it a few lines earlier. It is
the case the review left open, and it is the only account this file deletes.

### The tests, and what they refuse

`tests/test_post_deploy_invitation.py` — five, with the connection and the
invite helper doubled, which is the only safe way to test a path whose defect
was deleting real accounts. No database, no mail. The rule they hold is blunt
on purpose: **no invitation path may issue a delete against `auth.`**

Mutation-checked against both unsafe shapes:

* the F65 shape → the mixed-case test fails with the defect itself, naming the
  pre-existing id:

```
[('select id from auth.users where email=%s', ('Existing@Example.test',)),
 ('delete from auth.identities where user_id=%s', ('11111111-...-555555555555',)),
 ('delete from auth.users where id=%s',          ('11111111-...-555555555555',))]
```

* normalise the lookup but keep the delete — the half-fix the review warned
  against — → the race test still fails, with the deletes present. A
  normalisation that presents itself as the fix does not pass these.

### The acceptance ceiling, stated rather than implied

The review is right about this too, and my earlier write-up should have said
it: the upload goes in with the SERVICE key and the object's ownership is then
attributed to the uploader. That proves the credential works and the worker can
read the file back — the F59 failure — and it does **not** prove browser upload
RLS or browser sign-in. Those are the member's own session doing the upload,
which this file deliberately has none of.

### On "do not run it against real users yet"

It was not run against any. The two local exercises used
`post-deploy-invitee@comrade.invalid` — RFC-2606 reserved, on the local stack,
where GoTrue delivers to inbucket — and the account seeded for the second
branch was removed by that scratch harness, not by the check. Nothing in a real
project was touched, and the operator instruction now reads accordingly:

> Add `-e COMRADE_CHECK_INVITE_EMAIL=<an address you have authorised>` to
> include the invitation. The account it invites is LEFT IN PLACE — remove it
> yourself if it was not wanted.

### Unchanged

F59 itself. The credential is issued in the project's own dashboard, under
Settings → API Keys, and must come from the project the host's `SUPABASE_URL`
names — the 401 body recorded earlier carried Supabase's own hint that the key
"might also be owned by another Supabase project", which is what a key from the
wrong project looks like. Nothing in this repository can mint one.

### Gate

```
uv run pytest -q                            1766 passed, 8 skipped, 0 failed
tests/test_post_deploy_invitation.py          5 passed
```


## Nineteenth independent review — 2026-09-16

F65 is locally verified closed. The invitation acceptance function no longer
deletes identities or accounts on any branch. Its case-insensitive existence
lookup is used only for reporting; a user appearing after that lookup is also
preserved. The designated fixture teardown is separate from this path.

Independent verification:

```
python -m pytest tests/test_post_deploy_invitation.py tests/test_supabase_key_check.py -q -p no:cacheprovider
16 passed in 3.74s
```

The five new invitation tests cover mixed-case existing email, concurrent
appearance, normalized reporting, credential rejection and the opt-in default.
Code inspection confirms removal of account deletion rather than merely a
more permissive existence guard. No new actionable finding in this targeted
repair. No mail sent, real accounts changed, full backend gate rerun, or
production acceptance performed by this review.

F59 remains open: making the acceptance script safe and able to detect a
rejected key does not repair the pilot credential. Restore and verify that
credential, then check actual uploads/invitations and deployed continuation.


## F59 production credential repair — 2026-09-16

User supplied the valid credential in local `.env` and authorized applying it.
Validated Auth and Storage against the pilot's configured project before any
host change: both HTTP 200. Transferred the value using a temporary RSA-OAEP
public key generated on the host; SSM received ciphertext only. Plaintext was
not printed. Updated only SUPABASE_SECRET_KEY in `/opt/comrade/.env` and
recreated api, agent-worker and pipeline-worker from their existing images.
The temporary private-key directory was removed. A root-only rollback copy of
the prior environment remains at `/opt/comrade/.env.f59-before-key-update`.
No source deployment, merge, or commit was performed.

Independent production evidence:

- Each of api, agent-worker and pipeline-worker: Auth admin probe 200;
  Storage bucket probe 200 using its actual loaded credential.
- Public HTTPS root 200; `/api/ready` 200 with all eight deployed checks ok.
  The newer ninth advisory check remains uncommitted; direct probes establish
  credential acceptance independently of that diagnostic code.
- Executed the reviewed Storage acceptance helper in a one-off migrate
  container, with fresh random fixture IDs: upload succeeded, the running
  pipeline worker marked the document ready, and expected parsed text matched.
  No inline-content shortcut; object ownership was attributed for the fixture.
- Teardown reported no failures; team/profile/auth-user/identity/run/consent
  counts all zero. Uploaded object cleanup succeeded by exact path.
- Queried failed parse_document jobs: none; no jobs needed requeueing.

SSM credential application: `88b7eea0-e438-42df-8346-f914ce50a3d4`.
SSM production verification: `a151292a-1a9e-40ed-9c1c-a8bfc2662640` (Success).
Failed-job inspection: `f24c26ea-b38b-4196-929e-e89a80e97a35` (Success).

F59's rejected-credential defect is repaired and live Storage ingestion is
verified. Invitation email delivery was NOT exercised: no recipient was
authorized. Browser upload RLS, full external scenario, browser continuation
and latency acceptance remain separate. This verification sent no email and
changed no real user's account.


## F66 — resumed turns reuse persisted step numbers (2026-09-17)

**P1. Reproduced on the pilot; fixed locally, deployment pending.**
Browser sign-in and browser Storage upload/ingestion both passed after F59.
A real task approval executed the task, then the resumed agent run failed.
Run `5f81a75f-4021-4b58-a8c8-0464b5b29b25` already held steps 0–4;
`stream_turn` reset `all_steps` and attempted to append step 0 again.
The worker logged `UniqueViolation: agent_steps_run_id_seq_key`.
This affects continuation after human decisions and recovery of partial runs.

Fix: read `coalesce(max(seq) + 1, 0)` through the team-scoped agent session
and offset this segment's generated steps by that value. Keep the unique
constraint and existing worker lease fence; never replace previously emitted
steps or seed from the unused `current_step` column.

`tests/test_agent_resume.py::test_resumed_runtime_appends_after_persisted_steps`
uses actual enqueue/claim/pause/approve-or-reject/reclaim operations and the
real runtime with a deterministic model event. Both cases failed before the
fix with the production UniqueViolation and pass afterwards. Persisted steps
are exactly `[0, 4, 5]`, distinguishing the maximum from a row count.
Eight resume tests and 53 related run/stream/cancellation/budget tests passed.
The first test draft passed an EnqueuedTurn subclass where the real worker
passes a plain string; that setup error was corrected before measuring the
actual duplicate-sequence regression.

Baseline canonical gate exited 0: browser journeys 8 passed; real GitHub 2
skipped without PAT. Realtime needed its existing retry. The final gate with
`--with-agent-eval` is now running against the sequence fix.

Live ordinary questions, before this source deployment: 3/3 grounded answers
(tasks, membership, uploaded mascot fact), 34.926s / 32.995s / 35.478s.
This establishes a small successful sample, not a reliability or latency SLA.
Invitation mail remains untested because no recipient was authorized.


### Release acceptance follow-up — 2026-09-17

The gate against F66 passed every standard lane: 1768 backend passed, 8 skipped,
21 deselected; frontend 229 unit/component and 21 integration; browser 8;
real GitHub 2 skipped. Live usefulness also passed 4/4 on that invocation.
The extended gate exited **1**, not 0: the scenario's two-minute clone wait
expired before the five-minute workspace discovery interval. No sync job had
been queued. Increased only the scenario's wait to seven minutes (discovery
plus clone). The product can still take up to five minutes to discover a newly
connected repository; faster discovery remains a UX improvement.

On resuming, Docker Desktop was stopped. After restart Windows reserved ports
54257–54356, preventing Supabase's original published ports from binding.
A normal stop/start preserved the local database backup but initially failed
to bind 54322. Local Supabase ports and loopback URLs were moved to 5532x;
these machine-only config/env changes are excluded from the release. The
fixture retained all four members; 22 resume/scenario tests passed afterwards.

A repeated live-usefulness lane failed 1/4: the wiki question returned
"The chat doesn't show a decision on the team mascot" despite the seeded
wiki fact. This is NOT erased by the earlier 4/4 pass. The prompt explicitly
instructed that answer after an empty chat search, conflicting with its wiki
lookup rule. Removed that premature fallback and required the relevant wiki
page before declaring a decision unknown. Existing live acceptance is the
behavioral check; this prompt repair is not a guarantee of model reliability.
The full gate is being rerun with that change and the corrected clone wait.


### Gate isolation follow-up — 2026-09-18

The final2 gate exited 1: 1767 backend passed, one failed. The repository
discovery wiring test called the real global worker tick; its chat sweep
enqueued 19 unrelated local teams' compile jobs, spending real model calls
and exhausting the bounded drain before the fixture clone ran. This was a
test isolation defect, not evidence that the sequence fix broke cloning.

The test now permits only its repository maintenance clock, restricts its
claim SQL and sync enqueue to TEAM_A, disables unrelated expired-lease
maintenance, and asserts all foreign job ids/statuses/attempts are unchanged.
It still reaches discovery through the actual tick, real claim/handler/git
clone, and agent visibility. Two targeted connection tests passed. The
canonical extended gate is rerunning; no source deployment yet.

The two direct repository-sweep tests also assumed no foreign repositories
existed. Their assertions now name the fixture repository exactly, including
the positive retry assertions (which previously could pass on another team's
queued repo). All 48 repository/resume/scenario tests passed. An unrelated
Python 3.13 server occupied :8000; stopped only after the user's explicit
authorization. The final4 canonical extended gate is running.

Production pre-release backup created successfully in 44.5s on 2026-09-18:
`/var/backups/comrade/pre-release-20260918T044304Z/20260918T044305Z-88f2f1fe/`
contains globals.sql and database.sql. This new backup has not been separately
restored; earlier restore evidence is not claimed as a restore of this file.
Current api/frontend/agent-worker/pipeline-worker image IDs and sandbox image
were tagged `rollback-50ba2ec`. SSM command
`f993740e-49d3-4737-92bb-1f911508bbb5` succeeded.
GitHub App installation 158514342 can access Ranveersingh1113/test (HTTP 200).


## F67 — effect callback replay crashes and caches recoverable PR refusal

P1, found by the real four-person scenario on 2026-09-18. Final4 passed
1768 backend, frontend 229/21, browser 8 and live usefulness 4/4; its extended
gate still exited 1. Clone succeeded, generated snake.py ran in the sandbox,
then run 30324594-a5b0-474a-bc32-8d9e6a605b62 failed when repo_propose_pr was
called again after verification. The initial unverified proposal's error was
cached as completed; ADK invokes after_tool_callback even when before_tool
returns a cached result or refusal. complete_effect then tried to complete
a row already completed (or never claimed), raising LookupError.

Fix: per-tool-call context tracks whether this invocation actually claimed an
effect. Only those calls complete one. No shared session flag, no overwrite
of successful cached effects, no swallowed missing-claim errors. PR verification
refusal alone explicitly marks effect_not_started before patch capture or
consent creation; the durable claim may atomically restart that completed
refusal under the existing run/worker ownership fence. Other errors/writes
remain cached. No schema change.

New deterministic tests use the REAL ADK Runner and plugin lifecycle with a
scripted model, real effects database and counted tool body: cached and
capability-refused calls both reproduced the exact failure before repair;
the recoverable refusal test checks two attempts followed by a consent result.
The actual PR tool test proves its marker is returned before capture/proposal.
A wrong worker cannot reclaim the marked refusal. Focused tests passed.

## F68 — resolved consent remains pending in durable continuation

P1, reproduced with actual approval and rejection. The effect result retained
its original pending status after the card resolved. A repeated tool call
could return that stale result and park again on a card already decided;
_permission_wait also paused even when explicitly given executed/rejected.

Fix: approve, edit-and-approve and reject refresh the matching completed
effect's result in the SAME transaction that requeues the waiting run. Retain
consent_id, add current status/action result or rejection reason. Runtime waits
only for pending results (legacy missing status still means pending).
Regression uses real claim/proposal/pause/human-resolution/reclaim and checks
resolved cached status for all three routes, then exact persisted sequences
[0,4,5] and completion. The original three cases failed before the repair.
51 callback/verification/consent/concurrency tests passed, followed by 34
resume/consent-loop/column-guard/tier tests including edited approval and
wrong-worker retry refusal. Full scenario and deployment acceptance remain
pending; the standard gate will run again after live scenario verification.

## F69 — resolved approval repeats external work

P1, found by the real GitHub scenario. After approval successfully opened PR
#13, the resumed model ignored the resolved continuation result, edited again,
and opened PR #14. It continued proposing actions until the hourly token budget
stopped the scenario. The durable result was correct; asking a nondeterministic
model what to do with an already completed single action was the defect.

Fix: an executed PR is terminal, so its resumed run records a deterministic
completion (including the PR URL) and ends without another model call. A
rejection also ends the action. Other approved tools still resume: one request
may legitimately create several tasks. A plan with unfinished steps likewise
resumes normally. Approval, edited approval, rejection, and PR regressions use
real run/effect/consent rows and preserve the next durable sequence. Focused
resume/callback/scenario tests: 28 passed. The first live rerun proved PR replay
stopped, but also exposed and rejected the initially over-broad "all approvals
are terminal" rule before deployment.

## F70 — healthy production readiness exceeds the deploy probe timeout

P1, found by deploying `f713b06`. The production `/ready` response completed
successfully in 10.6–10.9 seconds, but `deploy_host.sh` killed each request at
10 seconds. Twenty-four healthy responses were discarded and GitHub Actions
reported a failed deployment after the new containers were already serving.

Fix: the probe now allows 30 seconds, longer than the endpoint's four database
role and two Supabase-plane network budgets. Retries drop from 24 to 10, keeping
the overall failure deadline near its previous value. The focused deployment
suite passes; production redeployment is pending.
