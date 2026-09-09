# Production readiness — work ledger

Companion to `2026-09-06-production-readiness-master-plan.md`. One entry per
task, per that plan's **Work ledger** rule: changed files, the failing
regression, what passed, review findings, migration/rollback impact, and the
remaining ceiling.

Branch: `claude/t01-baseline` (cut from `codex/production-boundaries`).
Nothing here is merged, pushed, or deployed.

---

## Baseline recorded at T01

`uv run pytest -q` → **981 passed, 6 failed**.

| failure | attribution |
|---|---|
| `test_deploy_host_script.py` ×5 | pre-existing; codex's in-progress diff. Fixed by T05 |
| `test_readiness::test_ready_reports_each_check_by_name` | **not a regression** — `/ready` correctly caught an unapplied migration of mine |

Frontend: build ✅, lint ✅, 163 tests ✅.

---

## T01 — Trustworthy baseline · `f454900`

**Changed:** `scripts/gates.sh`.
**Regression:** the gate ran `npx tsc --noEmit`; the deploy runs `npm run build`
(`tsc -b`). Different config resolution, so the lane was green for weeks while
the production image could not be built.
**Proof:** same deliberate type error — `tsc --noEmit` exits **0**, `npm run
build` exits **2**. Fixture restored, build green.
**Also:** reviewed the interrupted `test_deploy_host_script.py` diff and judged
it safe — `deploy_host.sh` invokes only `docker`, `git`, `mkdir`, `echo`,
`sleep`, and every side-effecting one is faked inside `tmp_path`. Confirmed the
scenario scorer grades rows/effects rather than prose, and `team_propose_batch`
is still gone.
**Migration/rollback:** none.
**Ceiling:** `npm run lint` passes with warnings, not clean.

## T02 — Preview browser origins · `0cd9411`

**Changed:** `server/previews.py`, `server/app.py`, `shared/config.py`,
`docker/Caddyfile`, `.env.example`, `docs/deployment.md`,
`supabase/migrations/20260906150000_preview_grants.sql`,
`tests/test_preview_origins.py`.
**Regression:** previews were served from Comrade's own hostname at
`/previews/<id>/`. A browser's boundary is the ORIGIN, so a development server —
written by a model, running unreviewed code — could read `localStorage` and take
the member's Supabase session.
**Design:** one hostname per process on a separate domain; single-use launch
grant traded for a host-scoped `HttpOnly` cookie with **no `Domain`**; access
rechecked against the database on every request.
**Passing:** 46 preview tests, 31 server tests.
**Review finding:** the same-origin route and its `mint`/`verify`/`authorize`
scheme were **deleted, not disabled** — an unused function is one import away
from being a route, and the plan's rollback note forbids restoring it.
**Migration/rollback:** additive (`preview_grants`). Rollback = unset
`COMRADE_PREVIEW_DOMAIN`, which disables previews.
**Ceiling:** 🔴 the plan's real-browser `localStorage` sentinel is **not done** —
it needs two real hostnames with TLS, which local dev cannot provide.

## T03 — Preview networks and setup egress · `42a7ba0`

**Changed:** `agent/processes.py`, `agent/sandbox.py`, `docker-compose.yml`,
`shared/config.py`, `tests/test_sandbox_processes.py`, `tests/test_repo_deps.py`.
**Regression:** a single shared `comrade-preview` network — introduced by my own
T02 fix — put every team's development server on one segment, able to reach each
other **and the API container**, which was also on it.
**Design:** one `--internal` network per process, created at start and removed at
stop, with only the proxy attached. The network is read back after creation:
"exists" is not "has no route out".
**Also:** setup egress enforced. That phase runs a repository's build hooks as
root and had the whole internet; it now runs on a network with no gateway where
the only route is a registry proxy, so direct-IP, DNS, IPv6, redirect and
`169.254.169.254` all fail for the same reason.
**Passing:** 78 passed, 6 skipped.
**Review finding:** `PLANNED_SETUP_EGRESS_ALLOWLIST` deleted and its
"pins-what-is-true" test **inverted** — unconfigured now means *no setup*,
because the old default was unrestricted egress.
**Migration/rollback:** none. Rollback = stop starting previews.
**Ceiling:** 🔴 no two-container Linux integration test proving cross-team
unreachability. Unit argv assertions are supplemental, as the plan says.

## T04 — Preview HTTP and browser behaviour · `402a37e`

**Changed:** `server/previews.py`, `server/app.py`,
`frontend/src/components/PreviewBar.tsx`, `frontend/tests/component/PreviewBar.test.tsx`.
**Regressions:** responses were buffered then **sliced** (half a bundle, served
200); `content-encoding` contradicted a decoded body; WebSockets returned 501 so
a preview loaded once and never updated; `window.open` after an `await` is
popup-blocked, so the button silently did nothing on first click.
**Passing:** 32 preview tests, 6 new PreviewBar tests, frontend build.
**Review finding:** `websockets` already ships with `uvicorn[standard]`, so
proxying needed no new dependency. The socket re-authorizes while open and has a
lifetime ceiling.
**Migration/rollback:** none.
**Ceiling:** 🔴 no real-app browser check (JS/CSS, navigation, HMR, redirect) —
the plan is explicit that initial HTML alone is insufficient evidence.

## T05 — Packaged execution and release ordering · `29ef71c`

**Changed:** `docker/app.Dockerfile`, `docker-compose.yml`,
`scripts/deploy_host.sh`, `.github/workflows/deploy-pilot.yml`,
`.env.example`, `docs/deployment.md`.
**Regression:** 🔴 **the app image contained no Docker CLI.** `repo_run`,
`process_start` and every dependency install would have failed in production
with "Docker is not available" — which reads like a daemon problem, not a
missing package. Verified fixed: `docker --version` → 29.8.0 in the image.
**Also:** `pipeline-worker` runs dependency setup but had no socket. Both
workers now get the socket and `COMRADE_DOCKER_GID`; the API gets neither.
**Also:** release order was `up -d --build` *then* migrate — activation before
migration, so a failed migration left the stack on new code against an old
schema. Now: lock → exact SHA → build → migrate `--no-deps` → activate →
readiness.
**Passing:** all 5 previously-failing deploy tests; 37 passed, 6 skipped.
**Migration/rollback:** none. Rollback = redeploy previous SHA; safe only while
the schema stays backward compatible.
**Ceiling:** 🔴 not smoke-tested against a disposable Linux stack; the
Docker-CLI fix is verified by `docker --version`, not by a real `repo_run` in
the packaged image.

## T06 — Truthful, restart-safe process lifecycle · `e5edb6d`

**Changed:** `agent/processes.py`, `pipeline/worker.py`, `server/app.py`,
`supabase/migrations/20260907100000_process_lifecycle.sql`,
`tests/test_sandbox_processes.py`.
**Regressions:** container name written *after* launch (a crash left a container
under a name no row had seen); failed start leaked its network; `stop()` recorded
a stop it never confirmed; nothing reconciled a crashed server, so rows said
`running` forever; idle was the only ceiling; and the FK cascade **deleted the
evidence** — removing a thread removed the only record of a running container.
**Design:** intended name persisted pre-launch; `reconcile()` asks the daemon;
confirmed removal before recording; absolute lifetime alongside idle; `touch()`
on accepted preview requests; a `before delete` trigger writes container and
network into `sandbox_cleanup` before the cascade, drained by the worker.
**Passing:** 89 tests.
**Migration/rollback:** additive (`sandbox_cleanup` + trigger). Rollback = drop
the trigger; orphan reclamation stops, nothing else changes.
**Ceiling:** 🔴 no termination-at-each-boundary test against a real daemon.

## T07 — Bounded output, disk and resource admission · `73e62da`

**Changed:** `agent/sandbox.py`, `agent/processes.py`, `pipeline/repo_sync.py`,
`shared/config.py`, `tests/test_repo_run.py`, `tests/test_sandbox_processes.py`.
**Regression:** 🔴 `subprocess.run(capture_output=True)` on an UNTRUSTED
command buffered a team's entire output in memory and clipped it afterwards.
`yes`, a test suite printing in a loop, or one line with no newline defeated it
— clipping at the end is far too late. Memory growth was whatever the
repository chose.
**Design:** `BoundedOutput` keeps a head and a tail and counts the middle, so
memory used is the budget rather than the output. Both streams drain in their
own threads, because a process writing heavily to stderr while nobody reads it
fills the pipe and blocks — a sequential reader deadlocks on it. The CONTAINER
is killed by name on timeout or overflow, since killing the client leaves it
running.
**Also:** `/tmp` is a tmpfs, i.e. memory, and was unbounded — now sized. Added
per-team and host-wide process admission limits, because a preview holds a
container, a network and a CPU share for hours and the reaper only runs later.
**Also:** eviction refused to look before deleting. It now skips a workspace
with a live process (mounted into a running container) or uncommitted work (not
recoverable from GitHub, which is what makes the usual "a checkout is just a
copy" argument not apply), and reports unmeasurable files instead of letting an
undercount read as under budget.
**Passing:** 57 sandbox/run tests, 82 across eviction suites.
**Migration/rollback:** none.
**Ceiling:** 🔴 no fork-bomb / disk-fill / quota-contention integration run on a
Linux daemon; the bounds are unit-verified plus the existing Docker lanes.

## T08 — Reproducible dependency environments · `d77faca`

**Changed:** `pipeline/repo_deps.py`, `agent/processes.py`,
`agent/repo_tools.py`, `tests/test_repo_deps.py`,
`tests/test_sandbox_processes.py`.
**Regression 1 (mine):** 🔴 **a preview mounted no dependencies at all.**
`repo_run` gets the dependency volume; `process_start` did not — so `npm run
dev`, the entire reason previews exist, failed on missing modules for any
project that has them. The preview system could not run a real development
server. Now mounted read-only, behind the same ready/stale status gate
`repo_run` uses.
**Regression 2:** lockfiles were found and hashed and then **ignored** — the
script ran `pip install -r requirements.txt` regardless. That is
reproducibility theatre: the hash moves when the lock does, so the cache looks
right while the install resolves whatever the registry serves that day.
Replaced with a recipe table, lockfiles first, each carrying its own frozen
installer (`uv sync --frozen`, `poetry install --sync`, `npm ci`).
**Regression 3:** JavaScript was not supported at all — a Node project reported
`no-manifest`, which reads as "this project has no dependencies". Added, along
with an explicit `unsupported` status for Go/Rust/Ruby/Java, because "we do not
install your language" and "you have no dependencies" are different sentences
and only one is true.
**Regression 4:** the cache key omitted the image. An unchanged manifest against
a new base image reused wheels built for the old interpreter and reported it
current.
**Review finding:** my first attempt added a SECOND `environment_key`, shadowing
an existing one that already handled a subtle case — the commit is folded into
the key only on the `pyproject` path, because that installs the repo itself.
Deleted the duplicate and extended the original instead.
**Passing:** 73 passed, 6 skipped.
**Migration/rollback:** none. The changed cache key rebuilds every environment
once, which is the intended effect.
**Ceiling:** 🔴 no frozen-reinstall reproducibility run and no src-layout
edited-code check on a real daemon; recipe selection and key composition are
unit-verified only.

---

## Phase B exit gate

`uv run pytest -q` → **1018 passed, 6 skipped, 0 failed** (exit 0).

Against the T01 baseline of 981 passed / 6 failed: every baseline failure closed
and 37 tests added. Frontend build ✅, lint ✅, 169 tests ✅.

The gate proves the suite, not the deployment. Everything in **Standing
ceilings** below is still unproven — all of it needs a Linux daemon or a real
browser against two TLS hostnames.

---

# Phase C — Conversation and agent reliability

## T09 — Paginate without hiding new conversation · pending commit

**Changed:** `frontend/src/hooks/useMessages.ts`, `GroupRoom.tsx`,
`frontend/tests/component/useMessages.test.tsx`.
**Regression:** 🔴 the query was `.order('created_at').limit(500)` — ASCENDING.
A thread past five hundred messages fetched the OLDEST five hundred, so **the
newest message never appeared**. A busy thread silently stopped showing new
conversation, which is the worst way for a chat product to fail: it looks like
nobody is talking rather than like something is broken.
**Design:** newest page fetched descending, displayed chronologically; keyset
cursors on `(created_at, id)` for older pages; pages merged by id.
**Also:** the id is in the cursor and the sort because timestamps are **not**
unique — bulk inserts share them, and a cursor on `created_at` alone drops or
repeats those rows. Tested with 600 identical timestamps.
**Also:** compilations were fetched for the WHOLE TEAM on every refresh to
render a handful of cards; now only for loaded message ids, with errors
surfaced instead of swallowed.
**Also:** a request token discards a superseded response — switching threads
quickly used to land one conversation's history under another's title.
**Also:** the view force-scrolled to the bottom on every change, yanking anyone
reading history back down mid-sentence. It now follows only when already near
the bottom, anchors scroll position when prepending older pages, and offers an
"N new messages" affordance instead of deciding for the reader.
**Passing:** 6 new hook tests; 175 frontend tests; build ✅.
**Migration/rollback:** none — reuses the existing `(thread_id, created_at, id)`
index.
**Ceiling:** 🔴 no real-Supabase integration run for pagination, and no
browser-level scroll-anchor check.

### T10 — Reconnect browser to durable runs and prevent duplicate sends

**Changed:** `supabase/migrations/20260907110000_turn_idempotency.sql`,
`agent/run_queue.py`, `server/app.py`, `frontend/src/lib/agentApi.ts`,
`frontend/src/screens/GroupRoom.tsx`, `tests/test_turn_idempotency.py`,
`tests/test_run_stream_resume.py`,
`frontend/tests/component/turnResume.test.tsx`, plus the three existing
component/stream tests whose doubles held the old contract.

**Regression:** two failures from one design. The browser watched a run only
through the POST that started it, and that POST carried nothing identifying
the attempt.

1. A refresh, a sleeping laptop or a dropped proxy connection ended the
   stream and nothing reconnected — indicator off, no reply, no reason —
   while the run itself carried on, durable and leased, answering nobody. A
   finished turn and a severed one were the same observation.
2. An accepted POST whose response never arrived was indistinguishable from
   one that never landed. The room restored the draft, the member pressed
   send again, and the thread got the same question twice: two messages, two
   runs, two model bills.

**Design:** a `client_request_id` identifies the ATTEMPT and survives the
retry; `(team_id, sender_id, thread_id, client_request_id)` is unique where
present, so the retry resolves to the turn already accepted and comes back as
`duplicate` carrying its run. Watching a run is now GET `/agent/runs/{id}/stream`
with an `after_seq` cursor — the same call for attaching, reattaching and
rejoining after a refresh, instead of a live path and a lost one.

**Also:** the lookup alone is check-then-act. The **partial unique index** is
what holds against two simultaneous retries; the lookup only avoids raising.
Proven by a test that inserts the colliding row directly.
**Also:** a duplicate **releases its budget reservation**. Budget is reserved
before the turn is persisted (T-earlier's ordering fix), so without this every
uncertain send costs a turn that never reached the model. Confirmed
discriminating: the test fails with the release removed.
**Also:** `waiting_for_permission` / `waiting_for_user` arrived as `done`, so a
run parked on a consent card stopped the indicator and read as a turn that
died in silence — beside the card that would have continued it. Lifecycle now
has its own frame type, and `done` means finished.
**Also:** a follow that ends without a terminal frame reports `truncated`, not
success, and reattaches from its cursor (bounded, 5 attempts). Replaying from
zero would show the first half of an answer twice.
**Also:** navigation aborts the subscription **and nothing else**. The run is
durable; leaving a room must not quietly kill a turn a teammate is waiting on.

**Passing:** 7 backend idempotency tests, 6 stream-resume tests, 9 new
frontend tests; 184 frontend tests; build ✅; lint ✅ (warnings only).

**Migration/rollback:** expand-only, deliberately. The column is nullable and
the index partial, so existing rows and any client sending no id keep working.
The RPC gained a five-argument form **alongside** the four-argument one, which
survives as a shim: releases build → migrate → activate (T05), so between the
migration and the new image the running API still calls the old signature, and
dropping it here would have made that window an outage. A later release
contracts it away. The fifth parameter takes no default on purpose — with one,
a four-argument call matches both signatures and Postgres refuses it as
ambiguous. Rollback of the code under this migration is safe; a test pins the
old signature.

**Ceiling:** 🔴 no real-network evidence. The truncation path is exercised
against a mocked stream, not a killed TCP connection or a proxy timeout; the
retry-after-uncertain-POST path is exercised against a mocked failure, not a
genuine accepted-then-dropped request. Both need a real deployment to prove.
🔴 Reconstruct-after-refresh follows `queued`/`running` runs only; a run
already waiting on a decision surfaces through its consent card instead.

### T11 — Expose cancellation, steering, and permission continuation

**Changed:** `supabase/migrations/20260907120000_permission_continuation.sql`,
`shared/agent_runs.py`, `shared/consent.py`, `agent/run_queue.py`,
`agent/effects.py`, `agent/runtime.py`, `agent/history.py`,
`agent/permission_plugin.py`, `agent/sandbox.py`, `agent/repo_tools.py`,
`server/app.py`, `frontend/src/lib/agentApi.ts`,
`frontend/src/screens/GroupRoom.tsx`, `tests/test_permission_continuation.py`,
`tests/test_run_cancellation.py`, `frontend/tests/component/turnCancel.test.tsx`,
plus the steering and resume tests whose doubles held the old contracts.

**Regression 1 — a run waiting on a person kept its worker lease.** That lease
answers one question, "is the process holding this run still alive", and is
measured in minutes so a dead worker is noticed quickly. A member deciding
whether to approve an action takes minutes to days. So
`recover_expired_agent_runs` found the parked run, called it abandoned and
requeued it; the re-run proposed the same action, hit the same pending card and
parked again — three cycles, then `failed: worker lease expired`, with the card
still sitting there and the member having done nothing wrong. Approving it
afterwards executed the action against a run already declared dead, and
`_requeue_permission_run` (which matches only `waiting_for_permission`) resumed
nothing. Parking now releases the lease; the backstop is the card's own expiry,
which is the clock that measures the thing actually being waited on.

**Regression 2 — rejection resumed nothing.** Approve and edit-and-approve both
requeued the waiting run. Reject wrote `resolution_reason` and stopped, so the
run stayed parked. That column exists so the agent can read WHY on its next
turn rather than just THAT; there was no next turn.

**Regression 3 — there was no way to stop a turn.** `cancel_run` sat in the
queue module with nothing reaching it: no endpoint, no button. A member who
asked the wrong question, or watched a turn head somewhere expensive, could
only wait it out.

**Regression 4 — steering had no sender attribution.** It arrived as "New
participant message", so in a room where anyone can redirect the work, a
teammate's instruction was indistinguishable from the requester's own. The
run's identity and any approval it holds stay with the ORIGINAL requester
either way, which is exactly why the difference has to be visible in the text
rather than implied by it. Attributed on the rule `recent_turns` already used:
a thread with one participant has nobody to tell apart.

**Design:** cancellation is authorised by two different questions. Thread
access says the run is yours to SEE; requester ownership says it is yours to
STOP, and that half is enforced inside the UPDATE rather than read first and
written after, because a check in another transaction can be raced. The
browser does not draw the button for a run it did not request either — not as
the control, but so nobody is invited to try.

**Also:** `_finish` called `finish_run` first, and `finish_run` refuses a run
that has left `running` — on purpose, so a worker's late "failed" cannot
overwrite the answer a member gave. But it RAISED, so `finalize_usage` never
ran: the estimate stayed on the team's hourly bucket, charging them for the
turn they stopped. The status is not the worker's to write; the money still is.
**Also:** cancelling used to stop the LOOP and nothing else. The bounded runner
now polls a `stop` callable about once a second, so a stop reaches a container
that is ALREADY running instead of leaving a test suite to hold a host CPU for
the rest of its ten-minute timeout — the runaway-container problem T07 exists
to prevent, arriving through the one door it had left open. `cancelled` is
reported separately from `timed_out`: "you stopped this" and "your command ran
too long" are different things to tell somebody.
**Also:** resolving a consent card resumed the run on the server and the room
rejoined nothing, so the member answered and then watched a thread where
nothing happened. All three resolutions now re-follow the same run.

**Already correct, verified rather than rewritten:** reusable grants are
rechecked on every proposal (`_approve_with_thread_grant` runs inside
`propose_action` and tests scope, expiry, revocation, risk ceiling and
requester-only resolution), publication and departure stay allow-once
(`_REUSABLE_GRANT_RISK` covers only reversible member-scoped task work), and an
uncertain external effect is reconciled rather than repeated
(`claim_effect` raises `EffectUncertain`). Existing coverage:
`test_revoked_thread_grant_returns_to_allow_once`,
`test_thread_grant_rejects_other_resources_requesters_and_expiry`,
`test_high_risk_actions_cannot_create_thread_grants`,
`test_completed_effect_returns_its_saved_result_after_worker_restart`.

**Passing:** 10 cancellation tests, 7 permission-continuation tests, 3 new
steering tests, 3 new frontend tests; 1052 backend tests, 6 skipped, 0 failed;
187 frontend tests; build ✅; lint ✅ (warnings only).

**Migration/rollback:** widening only. `consent_queue.status` gains `expired`
(an unanswered card is not `cancelled` — nobody decided anything, and calling
it that puts a decision in someone's mouth), and
`recover_expired_agent_runs` is replaced in place. Old code against the new
schema is fine; the run-ending sweep is deliberately narrow — a run is failed
only when one of its cards actually expired and none is still answerable, so it
cannot race the gap between approving a card and requeueing its run.

**Ceiling:** 🔴 an in-flight MODEL call cannot be taken back; cancellation is
observed between steps, so a stop during a long generation lands after that
call returns. 🔴 The container kill is proven against a real subprocess but not
against a real Docker daemon. 🔴 Reconnect-after-refresh follows `queued` and
`running` runs; a run already parked on a decision surfaces through its consent
card instead.

### T12 — Add fair bounded worker concurrency and independent maintenance

**Changed:** `supabase/migrations/20260907130000_worker_concurrency.sql`,
`supabase/migrations/20260907140000_job_leases.sql`, `agent/worker.py`,
`agent/run_queue.py`, `agent/effects.py`, `agent/runtime.py`,
`agent/permission_plugin.py`, `agent/repo_tools.py`, `pipeline/worker.py`,
`shared/config.py`, `.env.example`, `tests/test_worker_concurrency.py`,
`tests/test_pipeline_leases.py`, plus the worker tests whose doubles held the
old contracts.

**Regression 1 — the agent worker was strictly serial.** `main()` ran one turn
to completion before claiming the next, so one long turn anywhere in the
deployment made every other team wait behind it. Nothing in the queue required
that: `claim_next_agent_run` already guarantees one active run per THREAD,
which is the ordering guarantee that matters. The serialisation was in the
worker loop alone.

**Regression 2 — a worker that lost its lease kept working.** `renew_lease`
returning False ended the renewer thread and told nothing else, and
`claim_effect` fenced on the run being `running` — which says nothing about
WHO is running it. After recovery handed a run to a second worker, the first
could still perform that run's effects: the same external action, done twice,
by two processes each believing they owned the job.

**Regression 3 — the pipeline had no claim ownership at all.** `_finish`
updated `where id = %s`. A worker whose lease expired, and whose job another
worker had reclaimed, stamped its result over the new claim; the second
worker's work was thrown away by the first one's late answer. There was no
column to fence on, which is why the fence did not exist.

**Regression 4 — a failed job retried instantly.** Straight back to `pending`
and claimable on the very next iteration, so three attempts burned in
milliseconds against whatever was already broken. Retrying only helps if
something has had time to change.

**Regression 5 — `tick()` drained the queue to EMPTY before any maintenance.**
Under continuous ingestion the chat sweep, the disk cap and the sandbox
reconciler never ran. Maintenance that only happens when the system is idle is
maintenance that never happens on a busy system.

**Design:** concurrency is threads, not an async rework — each turn is a
blocking model call with blocking DB work around it, leasing is already
per-run, and every slot needs its own identity because the fence is BY worker
id. The per-team ceiling travels inside `claim_next_agent_run` rather than
being checked around it, so two workers asking at the same moment cannot both
see a team one under its limit. Ownership fences use `is not distinct from`,
so a run or job nobody leased matches a null caller and nothing else — a fence
rather than a hole.

**Also:** the ceiling counts `running` only. A run parked on a consent card
holds no worker, and counting it would let a team with two pending cards lock
itself out of the very turns that would resolve them.
**Also:** bounding the drain would have turned a five-second poll into a
five-second full disk scan and Docker reconciliation — fixing starvation by
replacing it with a stampede. Sweeps now run on their own clocks (chat 30s,
processes 30s, workspaces 300s).
**Also:** the job's worker id is KEPT on a terminal row as provenance and
cleared on a retry, because a job going back on the queue belongs to whoever
claims it next.
**Also:** the pipeline lease was a flat thirty minutes with no renewal, so any
honestly-long job was declared abandoned while still running and a second
worker did it again. It is now renewed while the handler runs.
**Also:** `comrade_control` holds COLUMN-level grants on `jobs`, so the two new
columns were invisible to it until granted — the first run failed with
`permission denied for table jobs`. That is the design working, and it is why
adding a column to that table is never only a schema change.

**Also:** the sweep clock is process-global module state, so one test that
ticked silenced the sweep for every test after it — three `test_repo_sync`
tests went red in the full suite while passing alone. Reset per test in
`conftest.py` rather than remembered per file, because the leak is structural.
**Also:** the first version of the sweep-interval test measured the machine
rather than the gating: a tick does real workspace and Docker work, so three of
them can outlast a 30-second interval and sweep twice, honestly.

**Passing:** 10 concurrency tests, 10 pipeline-lease tests; 1072 backend tests,
6 skipped, 0 failed.

**Migration/rollback:** additive only. `claim_next_agent_run` gains a defaulted
second parameter, so the previous one-argument call still resolves during the
window between migrating and activating; `jobs` gains two columns with safe
defaults, so the running image keeps working.

**Ceiling:** 🔴 concurrency is proven by the claim behaving correctly under
sequential calls and by slot identity, not by a load test with real
simultaneous turns. 🔴 No measurement of what two slots cost the host in
memory or Docker pressure; the default of 2 is a judgement, not a measurement.
🔴 `test_the_worker_holding_the_claim_still_finishes_normally` failed ONCE and
then passed seven consecutive runs of its file: the claim found nothing where a
row had just been committed. Not reproduced, not explained, and recorded rather
than dismissed — a claim that intermittently misses committed work is exactly
the shape of bug worth remembering. T13 covers pool initialisation and is the
likely place it gets an answer.

### T13 — Make streaming, realtime, and pools scale predictably

**Changed:** `shared/db.py`, `shared/agent_runs.py`, `agent/run_queue.py`,
`server/app.py`, `frontend/src/hooks/useRealtime.ts`,
`frontend/src/hooks/useMessages.ts`, `frontend/src/lib/agentApi.ts`,
`frontend/src/screens/GroupRoom.tsx`, `tests/test_stream_scaling.py`,
`frontend/tests/component/useRealtime.test.tsx`.

**Regression 1 — the stream re-read the whole run five times a second.** The
cursor T10 added filtered what the browser was TOLD; the database was still
handed `where run_id = %s` and returned every step. A turn with four hundred
tool calls shipped four hundred rows per poll, per viewer, to find the one that
was new. The read now starts after the cursor.

**Regression 2 — a flat 200ms poll with no heartbeat.** A run waiting on a slow
model call cost exactly as much as one producing a step every tick, and a
connection that had died silently was indistinguishable from one where nothing
was happening. The wait now backs off while nothing changes and snaps back the
moment something does, with a heartbeat so silence stays legible.

**Regression 3 — every realtime event caused a full refetch.** Ten messages
arriving together — an agent writing its reply while two people type — meant
ten complete reloads of the thread, each throwing away the answer the one
before it had just fetched. Events are coalesced into one read.

**Regression 4 — a dropped websocket froze the room silently.** The
subscription's status was ignored, so a reconnect restored the pipe without
restoring anything that had come down it while it was broken; only a window
focus recovered those events, and nothing told the member their room had gone
quiet for a reason. Resubscribing refetches, and the room says it is
reconnecting.

**Regression 5 — lazy pools were built without a lock.** `if pool is None:
create` from two threads builds two pools; one wins the dictionary and the
other is orphaned — open, holding its minimum connections, absent from
`_pools`, and therefore missed by `close_pools()`. Harmless while the workers
were serial; T12 gave every worker several slots and a renewer thread, which
is what made this urgent rather than theoretical.

**Regression 6 — the two STREAMING endpoints called `require_membership`
directly on the event loop.** A database round trip, on the endpoints a room
holds open, with every other request on that worker waiting behind it.

**Design:** double-checked locking, so the hot path stays lock-free and only
the one-time construction serialises. The heartbeat counts the wait the loop
ASKED for rather than wall clock, so it keeps its meaning when the loop is
driven by a test. Realtime coalescing is trailing-edge: the first event opens
the window and everything inside it collapses into one refetch, and the window
closes so coalescing never becomes swallowing.

**Also:** the FIRST subscribe must not refetch — the caller has just loaded —
but every one after it must. That distinction is the whole fix for a dropped
socket, and it is one boolean.
**Also:** `max_connections()` reports what one process may hold, so sizing
Postgres is not an archaeology exercise across five role pools and a lock pool.
**Also:** the event-loop rule is guarded by a STATIC test that walks
`server/app.py`'s AST, because it is a rule about how the code is written and a
load test would only reveal it under load nobody runs by accident.

**Passing:** 12 stream-scaling tests, 5 realtime hook tests; 192 frontend
tests; build ✅; lint ✅ (warnings only).

**Migration/rollback:** none — no schema change. `get_run` gained a defaulted
third parameter, so every existing caller is unaffected.

**Ceiling:** 🔴 NO MEASUREMENT. The plan asks for DB queries and bytes per
viewer and p95 latency under representative load; what exists here is a
correctness argument and unit evidence that the cursor, the backoff and the
coalescing do what they say. The numbers that would justify the constants
(200ms, 2s, 15s, 80ms) have not been taken. 🔴 The realtime tests drive a
faked channel, not a real Supabase socket, so reconnect behaviour is proven
against the contract rather than against the service.

### T14 — Finish thread visibility and participant management UX

**Changed:** `supabase/migrations/20260907150000_thread_participation.sql`,
`frontend/src/hooks/useThreads.ts`, `frontend/src/hooks/useRealtime.ts`,
`frontend/src/screens/Threads.tsx`, `frontend/src/screens/GroupRoom.tsx`,
`frontend/src/components/ThreadRoster.tsx` (new),
`tests/test_thread_participation.py`,
`frontend/tests/component/threadVisibility.test.tsx`,
`frontend/tests/component/mocks.tsx`, plus the thread tests that encoded the
old rule.

**Regression 1 — team discussion was gated on the thread being TITLED
"General".** `active?.title === 'General' && ...` decided whether a room had a
team composer, so every other public thread was Comrade-only: a team could
open a thread everyone could see and find they could not talk to each other in
it, and renaming General silently removed team chat from the one room that had
it. What decides now is who can READ the thread — everyone, or the people in
it.

**Regression 2 — removals left no trace.** `thread_participants` records
`added_by` and `joined_at`, so an addition was audited. A removal deleted the
row and took the only evidence with it — and removal is the consequential
half, revoking a person's access to the thread's whole history, its runs, its
approvals and its previews.

**Regression 3 — neither threads nor rosters were published for realtime.** A
teammate creating a thread, renaming one, or adding somebody to one was
invisible until a reload; the list refetched on window focus and nowhere else.

**Regression 4 — everything restricted was labelled "Selected members",**
including a thread with exactly one person in it. "Selected members" describes
a small group; a thread nobody else is in is Private, and that difference is
the whole question somebody is asking when they scan the list.

**Regression 5 — a restricted thread's membership was set at creation and
never again.** No way to see who else could read what you wrote, no way to add
or remove anyone, and nothing warning that adding a person hands them the
thread's entire history rather than starting them at today.

**Design:** two creation buttons rather than one button and a mode — a thread
everyone can see is the common case and stays ONE CLICK; choosing who is in
one is rarer and earns a step. The roster lives in the room rather than a side
panel, because the room has three layouts and two of them have no side panel.
The audit table has no foreign key to threads (the evidence outlives the row,
as with `sandbox_cleanup`) but does have one to teams, so a tenant erasing
itself takes its audit with it.

**Also:** `ComposerMode` kept its OWN copy of the mode, seeded from a
hardcoded `defaultMode="team"`, while GroupRoom kept another. Two sources of
truth that agreed only while every thread had the same default — the moment
work threads defaulted to Agent, the composer said Comrade and the toggle said
Team.
**Also:** the delete trigger is guarded on the team still existing. Deleting a
team cascades to its participants, and the trigger would otherwise try to
attach an audit row to the team being removed — a foreign key violation that
made deleting a team impossible. Caught by the first test run.
**Also:** the removed person cannot read the record of their own removal.
That follows the thread's own rule rather than being an exception to it.
**Also:** `useThreads` already preserved its list on a failed refresh; a test
now pins it, via a one-shot select failure added to the supabase mock.

**Passing:** 9 participation tests, 11 thread-visibility tests; 203 frontend
tests; build ✅; lint ✅.

**Migration/rollback:** additive. A new table, a trigger, and two tables added
to the realtime publication — nothing existing changes shape.

**Ceiling:** 🔴 `tsc --noEmit` reported the roster panel as clean while
`npm run build` found two `Cannot find name 'thread'` errors in it: I had put
it in a component that has no such prop. That is exactly the gap T01 changed
the gate for, and I walked into it by typechecking with the weaker command.
Use `npm run build`.
🔴 No two-browser journey: invite, removal and revocation are proven at the
RLS and component level, not by two real sessions watching each other. 🔴
Owner management is display-only — the owner is shown and cannot be removed,
but ownership cannot be transferred.

### T15 — Consolidate daily UX failure and recovery states

**Changed:** `server/app.py`, `frontend/src/lib/agentApi.ts`,
`frontend/src/screens/GroupRoom.tsx`, `frontend/src/screens/Documents.tsx`,
`frontend/tests/component/failureStates.test.tsx`,
`frontend/tests/component/mocks.tsx`, `tests/test_document_reingest.py`.

**Regression 1 — `deleteForEveryone` awaited its update and looked at
nothing.** RLS refusing the delete produced no error, no message, and the
refresh underneath put the message straight back — so a refused delete was
indistinguishable from a UI that had not noticed the click.

**Regression 2 — `markOpened` ignored its result too.** Open-tracking drives
"who has read this", so a silently refused write meant the team was reading a
list that quietly understated itself.

**Regression 3 — the draft lived in component state.** Switching threads to
check something threw away whatever had been typed.

**Regression 4 — nothing stopped a second send.** A double click, or an
impatient press while the first was still in flight, asked the question twice.
The attempt id from T10 makes a RETRY safe; it does not make a second
deliberate press free.

**Regression 5 — the errors were written for whoever wrote the code.** "Agent
endpoint not reachable — is the backend running on :8000?" is a sentence about
somebody else's laptop: nothing the reader can act on, nothing a helper can
search for. Failures now log the cause against a short reference and tell the
member what to do.

**Regression 6 — a failed ingestion offered nothing to do about it.** The only
retry was `/ingest`, which takes the bytes as multipart, so a member had to
find the file and upload it a second time — and after a reload the browser no
longer had it. The ingest endpoint's own docstring had this queued as "a later
slice that removes the double upload".

**Design:** drafts are keyed per member AND per thread, because two people at
one machine must not inherit each other's half-written messages. The send
guard clears in exactly one place, which is why the body moved into `deliver`.
`/reingest` encodes bytes exactly as the upload path does — a pdf that arrives
base64 on one path and raw on the other is a parser bug waiting for whichever
path is used second.

**Also:** the first rewrite of the error text replaced EVERY 4xx with generic
copy, and the GitHub-callback test caught it. When the server wrote a sentence
FOR the member — "that installation does not belong to an account you can
administer" — that sentence is the actionable part. Generic text is for
failures nobody wrote a sentence for.
**Also:** a document row with no `storage_path` gets a 409 saying to upload it
again, rather than a confusing failure further down.
**Also:** the delete control only exists while its row is hovered, and moving a
synthetic pointer onto it drops the hover that renders it. A harness problem
rather than a product one, but worth knowing before the next such test.

**Passing:** 5 reingest tests, 7 failure-state tests; 1095 backend tests, 6
skipped, 0 failed; 210 frontend tests; build ✅; lint ✅.

**Migration/rollback:** none. One new endpoint, additive.

**Ceiling:** 🔴 no upload PROGRESS — the Storage client used here reports
none, so uploading shows an indeterminate state with the filename rather than
a bar. 🔴 No keyboard, screen-reader or narrow-layout journey: the plan asks
for focus-after-error and status announcements tested in a browser, and this
is component-level evidence only. 🔴 `/reingest` is proven against a mocked
download; the real Storage read has never run.

## Phase C exit gate

**1095 backend tests, 6 skipped, 0 failed. 210 frontend tests. Build and lint
green.** Six tasks, T10 through T15, and the pattern across them is worth
naming: every one was a case where the SYSTEM was correct and the account of
itself it gave was not.

- A durable run kept working while the browser had stopped listening (T10).
- A run parked on a person was reaped by a clock built for dead processes,
  and rejecting a card resumed nothing (T11).
- Two workers could each believe they owned one job (T12).
- A stream re-read four hundred rows to find one, and a dropped socket froze
  a room in silence (T13).
- A thread named anything but "General" would not let the team talk in it
  (T14).
- A refused delete looked exactly like a missed click (T15).

The through-line for Phase D: the ceilings are almost entirely about EVIDENCE.
No load measurement, no two-browser journey, no real Storage read, no
keyboard or screen-reader pass, no Linux Docker daemon. The code is argued
for; a lot of it has never been watched working.

# Phase D — Evidence and memory

### T16 — Make capture timely, bounded, and recoverable

**Changed:** `supabase/migrations/20260907160000_capture_watermark.sql`,
`pipeline/chat.py`, `pipeline/compiler.py`,
`tests/test_capture_reliability.py`, `tests/test_chat_memory.py`,
`tests/_seed.py`.

**Regression 1 — the trigger was message COUNT alone.** Five new group
messages, or nothing. A team that made one important decision and then went
quiet never reached the threshold, so the decision was never captured — and
"we decided X" is exactly the kind of thing a team says once. Age is a second
trigger now, and deliberately a floor rather than a bypass: a conversation
still being typed should not be compiled a sentence at a time.

**Regression 2 — batches were unbounded.** Every message past the watermark,
so a team returning to a fortnight of backlog produced one enormous transcript
in one enormous model call.

**Regression 3 — the watermark was a bare `created_at > since`.** Two messages
sharing a timestamp meant one was captured and the other skipped FOREVER. The
same defect T09 fixed in the message list, here in the path that decides what
a team remembers.

**Regression 4 — a row committed after the watermark snapshot but stamped
before it** sat below the watermark permanently: a transaction that began
earlier and committed later is invisible to a reader that has already moved
past its timestamp.

**Regression 5 — `extract_candidates` ended
`return list(parsed.facts) if parsed else []`.** `resp.parsed` is None when the
model's answer could not be read AT ALL, so a malformed response was
indistinguishable from "I read this and there was nothing in it" — and the
compile then wrote its row and ADVANCED the watermark, discarding that stretch
of conversation permanently. That is the worst of the five: a transient model
glitch silently ate a team's decisions.

**Regression 6 — every team-visible thread was ordered together by time**, so
two unrelated conversations reached the model as one exchange. A model asked
to extract decisions from that will happily invent the connection.

**Design:** grouping happens BEFORE numbering, in one place, so `source_index`
and the citation map refer to the order the model was actually shown. Thread
headers do not consume an index — the reader needs to know these are separate
conversations; the citation map must not change. `bound_batch` never returns
empty for a non-empty input: one message longer than the whole budget still
has to be compiled, or it blocks the watermark for good. The dedupe key is the
boundary ROW, not its timestamp, because two batches can share a timestamp and
keying on it alone made the second look like a duplicate and vanish.

**Also:** the sweep's SQL prefilter had to learn the age rule too. A prefilter
that disagrees with the authoritative check means the sweep never calls it and
the new trigger is unreachable.
**Also:** eight existing chat tests failed on the capture lag. Most needed
backdating — a message written this instant is not eligible yet — but two were
GLOBAL assertions (`sweep_chat_compiles() == []`) that cannot hold once any
team anywhere with old unswept messages is a candidate, which is the entire
point of the age trigger. Scoped to the teams they seed.

**Passing:** 11 capture-reliability tests; 1106 backend tests, 6 skipped, 0
failed.

**Migration/rollback:** additive — one nullable column. A null
`chat_through_id` still forms a valid keyset against the minimum uuid, so
compilations written before this migration resume correctly.

**Ceiling:** 🔴 the batch budget is CHARACTERS, not tokens. Characters are a
proxy that is wrong by a factor that depends on the language and the content;
the plan asks for estimated tokens. 🔴 The lag is five seconds, chosen rather
than measured against how long a transaction actually stays open here. 🔴 No
test of a crash between apply and job completion — the plan's last check —
because that needs a killed worker rather than a raised exception.

### T17 — Separate requests to Comrade from team decisions

**Changed:** `supabase/migrations/20260907170000_message_to_agent.sql`,
`pipeline/chat.py`, `pipeline/compiler.py`, `evaluation/extraction.py`,
`evaluation/extraction_set.py`, `tests/test_extraction_provenance.py`,
`tests/test_extraction_recall_live.py`.

**Regression 1 — the transcript carried no provenance.** It was
`[3] Name: message` and nothing else, so a line addressed to the agent —
"@comrade investigate switching to Postgres" — was indistinguishable from one
where the team settled something — "we are switching to Postgres". A request
to look INTO an option could be compiled into the wiki as a decision the team
had TAKEN, and the wiki is what the agent reads back as fact on every later
turn, so the error compounds.

**Regression 2 — the prompt did not distinguish asking from deciding.** It
excluded questions and banter, but "investigate switching the database" is
neither a question nor banter: it is a request, and it looks exactly like a
decision. Proposals, tentative assignments, reported statements and corrected
values all had the same problem.

**Regression 3 — the metric measured recall alone**, which is gameable in the
worst direction for this system: a prompt that extracts every sentence scores
1.0, and every sentence it writes into the wiki is something the agent reads
back as fact. The module's own docstring had flagged it — "If extras ever need
judging, that is a precision metric and a different labelling job."

**Regression 4 — explicit remember intent never reached the model.** The
codebase calls a fact a human pointed at "the highest-signal fact in the
system"; the compile knew the trigger and the transcript did not say so.

**Design:** agent-direction is recorded ON THE MESSAGE rather than derived from
`agent_runs` at read time. The first implementation did derive it and failed
immediately with `InsufficientPrivilege` — that table deliberately has no broad
grant (findings §4.1: private prompts and tool results), and widening it so the
memory compiler could ask one boolean question would have traded a real
privacy boundary for a convenience. Precision is scored against LABELLED TRAPS
rather than every unlabelled extra, because the labeller lists what must be
found and what must not be, never everything findable — penalising extras
punishes thoroughness, and thoroughness is the behaviour this system needs
most.

**Also:** the prompt now says explicitly that being addressed to Comrade does
NOT disqualify a decision. The obvious fix — "ignore anything aimed at the
agent" — would lose "Comrade, we have decided to switch to Postgres,
implement it", which is a decision and a common way to say one.
**Also:** the live precision test also asserts recall did not collapse, because
extracting nothing is the trivial way to score perfect precision.
**Also:** the mention pattern is kept beside the column as a second signal, for
messages written before the column existed.

**Passing:** 8 provenance and scorer tests; 1114 backend tests, 6 skipped, 0
failed.

**Migration/rollback:** additive — one boolean column defaulting false, and the
enqueue RPC replaced in place. Rows written before it read as not-agent-
directed, which the mention pattern still catches for the common case.

**Measured, not asserted.** The live evaluation was run against the real
model: both chat sources scored **recall 100%, precision 100%, zero false
positives**. It correctly kept "investigate switching the database" and "could
we move to Postgres" out of memory while extracting the decision, its owner,
and an IE11 negation that was addressed to Comrade — and dropped the corrected
date, the tentative assignment and the client's reported date. Overall stage-1
recall across the older set: 19/20 (95%), above its 80% floor.

The first live run reported the 21st as MISSED, and the label was wrong, not
the extraction: matching is whole-token, the extractor produced "the 21st", and
a bare `21` key cannot match it. Same shape as the auth/authentication case the
module already documents. An eval that reports a miss for a fact that was found
is an eval people learn to ignore, so the key is now `("21", "21st")`.

**Ceiling:** 🔴 Two labelled chat sources is a small set: it will notice a
regression, not characterise one. 🔴 One run is not a distribution — the plan
asks for REPEATED live evaluation, and a single 100% says the prompt handles
these six traps once, not that it is stable. 🔴 No assertion that private
threads stay out of promotion; that boundary is the fetch's
`visibility='team'` and is unchanged, but the plan asks for it to be pinned.

### T18 — Verify citations and fail safely on uncertain consolidation

**Changed:** `supabase/migrations/20260907180000_memory_trust.sql`,
`pipeline/compiler.py`, `pipeline/chat.py`, `pipeline/wiki.py`,
`tests/test_citation_trust.py`, `tests/test_compiler.py`.

**Regression 1 — excerpts were never checked.** The model's quote went
straight into `memory_citations` with nothing verifying it appears in the
source, so a GENERATED QUOTE BECAME EVIDENCE BY ITSELF — and the citation is
the one thing a member looks at to decide whether to believe a fact.

**Regression 2 — `if valid is None: action = "add"`.** A decision naming an
entry that does not exist, or belongs to another team, did not fail and did
not get rejected: it got PUBLISHED, as a brand new fact. A model that invented
an id has said nothing about where the candidate belongs, and inventing a home
for it is the system agreeing with the invention.

**Regression 3 — revisions superseded whatever was active at APPLY time**,
not the version consolidation read. Two compiles touching one entry meant the
second silently overwrote the first's judgement from stale context.

**Design:** an unsupported candidate is QUARANTINED rather than dropped or
published — written down so it can be reviewed, never active, so neither the
wiki nor the next consolidation treats it as established. Dropping it silently
would hide a model that is fabricating; publishing it is the thing this task
exists to stop; leaving it active-but-flagged would let it launder itself into
the wiki one round later, when the next compile reads it as context.

The version id for a revision comes from the SNAPSHOT, not the model. It is a
fact about what was read rather than a judgement, and asking the model to echo
it back would only add a way for it to be wrong.

Matching is normalised, not byte-identical: the model re-punctuates, and a
quarantine that fires on correct work is one everybody learns to ignore.

**Also:** a DATABASE TRIGGER was the only thing standing between a cross-team
citation and the wiki. My first pass treated "no such source under this team"
as "could not check" and let it through; the DB raised
`citation source must belong to the version team`. A message source that is
not this team's is now an explicit NOT SUPPORTED — a real quote from a source
this team cannot see is not support for a fact in this team's wiki.
**Also:** `test_apply_bad_target_falls_back_to_add` was a test asserting the
defect. Rewritten to assert rejection, with its docstring saying what it used
to claim, rather than quietly deleted.
**Also:** the diff card says how many facts were held back and why. A silent
quarantine is a silent loss.

**Trust vocabulary, deliberately modest:** `observed` means the excerpt was
found in a source this team can read. It does NOT mean the fact follows from
the source. Substring matching cannot establish entailment, and a column that
claimed it could would be worse than no column at all.

**Passing:** 9 citation-trust tests; 1123 backend tests, 6 skipped, 0 failed.

**Migration/rollback:** additive — one column with a default, so every existing
version reads as `observed`, which is what they were implicitly claiming.

**Ceiling:** 🔴 DOCUMENT AND GITHUB EXCERPTS ARE STILL UNVERIFIED. Those
sources are compiled from text held in memory at extraction time and never
stored, so apply cannot re-read them; `excerpt_is_supported` returns "could not
check" and they publish as before. The fabrication hole is closed on the chat
path only. 🔴 No review surface for quarantined facts: the diff card says how
many were held back, and nothing yet shows a member WHICH, or lets them
confirm one. 🔴 `confirmed` and `verified` exist in the vocabulary with no
path that sets them.

### T19 — Add compact durable thread working memory

**Changed:** `supabase/migrations/20260907190000_thread_working_state.sql`,
`20260907200000_compaction_sweep_grants.sql`,
`20260907210000_compact_thread_job.sql`, `agent/history.py`,
`agent/runtime.py`, `pipeline/compaction.py` (new), `pipeline/worker.py`,
`tests/test_thread_working_state.py`.

**Regression — the context window WAS the memory.** The agent's knowledge of a
thread was the last `agent_history_turns` messages and nothing else, so
everything said before that was gone: not summarised, not stored, gone. A
constraint stated a hundred messages ago — "we are not touching the vendored
fork", "the customer is still on Postgres 14" — was invisible to every later
turn, so the agent proposed what the team had already ruled out and somebody
had to say it again. Nothing survived a worker restart either: whatever a turn
had worked out lived in a prompt that no longer existed.

**Design:** pins are stored SEPARATELY from the summary. A rolling summary is
rewritten by a model every time it grows, and prose gets paraphrased a little
each round until it means something else — a constraint the team stated is not
a thing to paraphrase. Compaction is a JOB rather than part of a turn: it
costs a model call, and a member waiting for an answer should not pay for the
bookkeeping that makes the next answer better. The cursor and the summary move
together and only after the summary exists, because a cursor that advanced
first would silently drop everything it covered the moment the call failed —
the same shape as T16's extraction bug.

**Boundaries that pushed back, and were respected rather than widened:**
- Compaction reads messages AS THE REQUESTER. The first version read as
  `comrade_agent` and was refused: findings §4.1 deliberately revoked that
  role's select on `messages`. Compaction is not a reason to widen it.
- `comrade_control` can claim and finish jobs but CANNOT create them, so the
  sweep scans cross-team as control and enqueues per team as pipeline — the
  same split the chat sweep uses.
- Its column grants meant the sweep needed exactly two new columns, so the
  scan is a PREFILTER on `created_at` alone while `compact_thread` recomputes
  the range with the full keyset. An approximate count can queue a job that
  finds nothing to do; it cannot cause a message to be missed.

**Also:** the working state is datamarked on its way into the turn. A summary
is written FROM member text, and one that reaches the model unmarked is an
injection surface with a very long memory — it is replayed on every subsequent
turn of the thread.
**Also:** the summary prompt is told NOT to restate approvals, permissions,
paths or diffs. Those are recorded exactly elsewhere, and a paraphrase here
would be a second, weaker version of a thing that has to be exact.
**Also:** `test_the_worker_registers_a_handler_for_every_job_type` caught that
`pipeline.compaction` was not imported in the worker's `main()` — the exact
failure that test exists for, where a handler registers in the test process
and nowhere else.

**Passing:** 15 working-state tests; full suite below.

**Migration/rollback:** additive — a new table, two column grants, one widened
check constraint. A thread with no row reads as empty state.

**Measured, not asserted.** `_summarise` is stubbed in the unit tests, so the
prompt itself was run against the real model over a 105-message thread
(`tests/test_thread_summary_live.py`). Both constraints stated in the first two
messages survived a hundred messages of noise; the thread's self-correction was
carried forward WITHOUT the thing it corrected ("the exporter will ship first,
with the importer to follow"); the unanswered question was kept; and the whole
summary came to 395 characters against a 2,000 budget. That is the plan's own
first check for this task, and it now has an answer rather than an argument.

**Ceiling:** 🔴 One thread shape, one run. It shows the prompt can do this,
not that it does it reliably across the shapes real threads take. 🔴 No
concurrent-compaction test: two
workers compacting one thread would both write a summary, and the second's
cursor wins. The job dedupe key makes it unlikely, not impossible. 🔴 No UI:
the state is inspectable and correctable through RLS, so a member could fix a
wrong summary via the API, but nothing shows it to them. 🔴 `open_questions`,
`plan_version` and `pending` are in the contract and in the schema, and
nothing populates them yet.

### T20 — Bound retrieval cost and measure memory usefulness

**Changed:** `pipeline/wiki.py`, `agent/agent.py`, `agent/tools.py`,
`tests/test_retrieval_cost.py`.

**Regression 1 — the index loaded the whole wiki to print page names.**
`wiki_section` renders TITLES AND DESCRIPTIONS ONLY, and built them by calling
`all_active_pages` — every active fact of every page, each with its
`valid_from` and its first citation kind. On EVERY turn of EVERY thread. A
team with two thousand facts paid two thousand rows for a list of page names,
and the cost grew with the corpus forever.

**Regression 2 — opening ONE page cost the whole wiki too.**
`read_memory_page` called the same function and picked one page out of the
result in Python.

**Regression 3 — one oversized page bypassed every budget.**
`CONSOLIDATION_FACT_CAP` bounds what the COMPILER is shown; nothing bounded
what a single page could contribute to a TURN, so a page with a thousand facts
went into the prompt whole.

**Design:** `exists` rather than a join for "does this page have anything on
it" — a join would fetch the facts to answer the question, which is the bug. A
truncated index and a truncated page both SAY SO: showing part of the wiki in
silence teaches the agent the rest does not exist, and it will report that
absence to the team as fact.

**Also:** an existing test caught a hole I would have shipped. Entries that
predate pages have no `page_id`, and a metadata-only index over
`memory_pages` cannot see them — those facts would have disappeared from the
index entirely, which is the starvation this system is most afraid of.
`test_page_less_facts_still_surface` went red; the orphan bucket is now served
by both the index and the page reader.
**Also:** two of my own assertions were wrong and were fixed rather than the
code. One forbade the string `memory_versions` in the index SQL, but an
`EXISTS` against it is the right way to ask whether a page has content —
fetching `v.fact` is the bug, so that is what it forbids. The other counted
occurrences of the word "select"; what matters is that the statement count
does not grow with the number of pages, so it counts statements.

**Passing:** 8 retrieval-cost tests; 1146 backend tests, 6 skipped, 0 failed.

**Migration/rollback:** none — no schema change.

**Ceiling:** 🔴 NO BENCHMARK. The plan asks for a growing-corpus benchmark and
measured retrieval quality: paraphrase, negation and identifier cases, revision
recall beyond 400 facts. What exists here is a structural fix with tests that
prove the queries no longer read what they do not need — the number of rows is
bounded now, and nobody has measured the latency or the recall at size. 🔴 No
token attribution: the plan asks for tokens split across prompt, tool schema,
history, memory, files and output, and nothing counts them. 🔴 `memory_search`
is unchanged — it was already FTS-backed and bounded, but the "retrieve
broadly, rerank cheaply" half of the item is not built.

### T21 — Minimize ingestion data and align deletion/retention behavior

**Changed:** `supabase/migrations/20260907220000_document_parse_error.sql`,
`shared/storage.py` (new), `pipeline/compiler.py`, `server/app.py`,
`frontend/src/lib/agentApi.ts`, `frontend/src/screens/Documents.tsx`,
`frontend/src/screens/Setup.tsx`, `tests/test_ingestion_minimisation.py`,
`tests/test_document_reingest.py`.

**Regression 1 — the queue carried every team's documents past a role that
could read them.** `enqueue_document` put the WHOLE DOCUMENT in
`jobs.payload` — base64 for pdf and docx — and `comrade_control` holds
`select (… payload …)` on that table. That role is the cross-team maintenance
plane: it claims jobs, sweeps queues, reaps leases. It could read the contents
of every document every team had ever uploaded. The browser was uploading the
file TWICE, once to private Storage and once to the API, so the exposure
bought nothing at all. `handle_document_job`'s own docstring had the fix
queued: "Production will fetch bytes from Supabase Storage by the document's
storage_path instead."

**Regression 2 — nothing rechecked deletion.** A member could delete a
document and its contents would still be fetched, parsed and compiled into the
wiki afterwards. Checked before the fetch AND again before the apply.

**Regression 3 — no size limit anywhere.** A parser allocating a gigabyte is
the worker dying and every other team's jobs waiting behind the restart — one
team's upload becoming everybody's outage.

**Regression 4 — `.doc` was mapped to kind `docx`.** A legacy .doc is an OLE
compound file python-docx cannot read, so it uploaded, parsed to nothing, and
came back as "likely scanned or unsupported": a wrong explanation the member
could do nothing with.

**Regression 5 — `status='failed'` was the whole story.** A member could not
tell "this is a scan" from "this is too big" from "we cannot read this format"
— three problems with three different answers behind one blank wall.

**Design:** the download is STREAMED and counted rather than read whole and
measured afterwards, because measuring afterwards means the allocation already
happened, which is what the cap exists to prevent. `content_sha256` records
which bytes the job meant, so a file replaced at the same path between
queueing and running is detectable rather than silently parsed. Inline
`content` is still accepted: jobs queued by the previous image carry it, and a
release must not strand whatever was in the queue when it started.

**Also:** the API had its own copy of the storage read, added with `/reingest`
in T15. One copy now, in `shared/`, because the worker is the caller that
matters.
**Also:** two T15 tests asserted that `/reingest` DOWNLOADS the file. That was
right when it was written and is wrong now — the fetch moved to the worker —
so both were rewritten against the new contract with the reason in their
docstrings rather than quietly deleted. The binary-encoding assertion moved
with the behaviour it was guarding.

**My own mistakes, recorded:** a patch script asserted its way out BEFORE its
single `write_text`, so three edits reported as applied were silently
discarded; the tests still failing on the old behaviour is what caught it. And
the heredoc backslash-collapsing trap bit again on a `\x00\x01` literal.

**Passing:** 10 ingestion tests, 5 reingest tests; 1156 backend tests, 6
skipped, 0 failed; 210 frontend tests; build ✅.

**Migration/rollback:** additive — one nullable column. Old jobs with inline
`content` still run, so a rollback in either direction is safe.

**Ceiling:** 🔴 `content_sha256` is RECORDED AND NEVER CHECKED. The worker
does not compare it against what it fetched, so a file replaced at the same
path is detectable in principle and undetected in practice. 🔴 No retention
policy: the plan asks for retention over raw payloads, private steps, logs,
artifacts and exports, and none of that is built — only the document payload
shrank. 🔴 Nothing defines which derived FACTS are retracted when a source is
deleted; the facts compiled from a deleted document stay in the wiki with
citations pointing at something that is gone. 🔴 No orphan-upload cleanup, so
a Storage object whose DB insert failed is never collected.

# Phase E — Engineering workflow

### T22 — Connect tasks, work threads, and optional plans

**Changed:** `supabase/migrations/20260908090000_task_thread_link.sql`,
`shared/consent.py`, `frontend/src/lib/types.ts`,
`frontend/src/screens/Tasks.tsx`, `tests/test_task_thread_link.py`,
`tests/test_task_tools.py`.

**Regression 1 — tasks and work threads were unconnected.** `tasks.status` had
one vocabulary (proposed/confirmed/in_progress/done) and `threads.work_state`
had another (planned/active/waiting/review/done), with nothing joining them —
so a team doing a piece of work in a thread AND tracking it as a task had two
answers to "is this finished", which disagreed the moment either moved. There
was no authoritative status; there were two.

**Regression 2 — a leak the link would have created.** `au_tasks_select` was
`is_team_member(team_id)`: every member saw every task. Linking a task to a
RESTRICTED thread would have published its title, owner and deadline to people
who cannot open that thread — exactly the "including counts/previews" the plan
warns about. Caught while writing the migration, not after.

**Regression 3 — the column restriction on `task_update` ran only at EXECUTE
time.** The agent could queue a card asking to close work, a member could
approve it, and only then would it fail — somebody approving something that
cannot happen. Refused at propose time now, which is the rule `propose_action`
already applied to a tool with no executor at all.

**Design:** the TASK is the unit of work — it has an owner, a deadline, and a
human who decides when it is finished — so its status is authoritative and a
linked thread's `work_state` follows it by trigger. One writer, one truth: two
columns both claiming to say whether work is finished will disagree, and the
disagreement surfaces as a board that contradicts the thread it links to.

**Checked rather than assumed, and the answer was better than the plan
expected.** "Never auto-close work from plan completion or assistant prose" is
ALREADY TRUE: `task_update`'s permitted columns are title, description,
deadline and assignee, and `status` is deliberately not among them, so the
agent has no path to close work at all. Pinned by a test. A second line was
added at the grant layer for if that list ever grows — marking work done is the
one task outcome a person has to decide, and one "allow for this thread"
should not buy it.

**Also:** three existing database guards pushed back while the tests were
written, and all three were right — a task must START proposed, only the
ASSIGNEE may move it out of proposed, and a permission grant must be created
by its requester AND trace back to an executed consent. The tests were
rewritten to go through the real paths rather than around any of them.
**Also:** `test_a_task_update_touching_status_or_confirmed_at_is_rejected` went
red, correctly: it proposed a bad key and waited for the EXECUTOR to refuse.
Split into two tests for two entry points — the proposal, refused up front, and
`edit_and_approve`, where a HUMAN injects the bad key into an already-pending
card and only the executor can catch it.

**Passing:** 12 task-thread tests; full suite below; 210 frontend tests; build ✅.

**Migration/rollback:** additive — a nullable column, two narrowed policies and
a trigger. Existing tasks have no thread and are unaffected, including their
team-wide visibility.

**Ceiling:** 🔴 THE BOARD CARD IS NOT BUILT. The plan asks a card to show
owner, due date, current run, blockers, PR/CI, last activity and plan
progress; what exists is the link and one affordance to follow it. 🔴 Nothing
CREATES the link yet — no UI attaches a task to a thread, so the column is
correct and unreachable from the product. 🔴 No historical migration of
implicit links, because there were none to migrate: nothing had ever recorded
one.

### T23 — Tie PR verification to the actual proposed workspace revision

**Changed:** `agent/repo_tools.py`, `tests/test_verification_binding.py`.

**Regression — verification was an in-memory COUNTER.** `repo_edit`
incremented `repo_edit_generation`; a `repo_run` that exited 0 stamped the
current count into `repo_verified_generation`; the proposal gate compared the
two. It knew that "a run happened after the last edit" and nothing about WHAT
was checked or WHAT it was checked against. Three ordinary ways through it:

  * **`python -c 'pass'` exits 0**, and so do `true`, `:` and `echo ok`. The
    old rule rejected only `--help`, `--version` and a bare `make`, so any of
    them marked the tree verified. The plan names this example by name.
  * **A command can change the tree.** A formatter, a codegen step, a build
    that writes files — none of them touch the edit counter, so the check that
    ran BEFORE the mutation still vouched for the tree AFTER it.
  * **A restart rebuilds the ADK session**, so a check made in a previous
    attempt left no record — and the counter, rebuilt with it, could not tell
    "checked earlier" from "checked never".

**Two errors of mine, caught by the existing tests.** The gate first computed
its digest from `repo_checkout` — the TEAM's checkout — while every other
repository tool works in the THREAD's tree, so a genuine check never matched
its own proposal and the gate refused everything.
`tests/test_repo_verification_gate.py` says so in its own fixture; I had not
read it carefully enough. And I had claimed a resumed run with zero edits
counted as verified. When a turn changes nothing, "go run a test" is the WRONG
refusal — it sends a member looking for a change that was never made, and the
empty-diff rule already says something actionable. A checkout can also carry
work this turn did not do, and demanding the agent verify somebody else's
uncommitted files is not an improvement. The claim was withdrawn from the test
and its docstring rather than left standing.

**Design:** the rule is split by question, which is clearer than the single
predicate it replaced. "Did this turn change anything?" is the edit counter,
which answers it correctly. "Is what was checked what is being proposed?" is
the patch digest, which is the part that was missing. Verification binds to
the PATCH — the diff against HEAD plus the
untracked files, hashed — taken AFTER the command ran, so a check that mutates
the tree records the tree it left behind. One mechanism closes all three
holes, because it answers the question the gate is actually asking: was THIS
change checked. The record carries the command and the digest, so what
verified a proposal is now a fact rather than an inference.

**Also:** the command filter is deliberately weak, and says so in the code. No
static rule can tell a meaningful test from a shallow one; what it can do is
rule out commands that provably read nothing from the project, which is where
the old gate let everything through. Inline code (`python -c`, `node -e`)
counts only when the snippet names something in the project.
**Also:** a failed digest measurement returns a UNIQUE value, so the gate
refuses rather than waving a change through on a measurement it could not
take. Unknowable is not verified.
**Also:** the counter rule is kept as `_unverified_legacy` with its original
reasoning intact, rather than deleted — it explains why zero edits is not
unverified, which the new rule inherits.

**Passing:** 18 verification-binding tests; 47 repo-tool tests; full suite
below.

**Migration/rollback:** none — no schema change. Turn state gains one key.

**Ceiling:** 🔴 NO PROJECT-DECLARED CHECKS. The plan asks for project-declared
checks or an explicit reviewed verification policy; this rules out provable
no-ops and accepts everything else, so `pytest tests/test_nothing.py` still
verifies a change it never exercised. 🔴 The digest is computed with `git` in
a subprocess against the checkout — never measured on a large repository, and
it runs on every proposal. 🔴 No test against a real GitHub PR, and no
coverage of a background process writing to the tree after a check: the
mechanism handles it by construction (the digest moves) but nothing exercises
that path.

### T24 — Add scoped thread attachments and explicit knowledge promotion

**Changed:** `supabase/migrations/20260908100000_thread_attachments.sql`,
`20260908110000_team_document_default.sql`, `pipeline/compiler.py`,
`tests/test_thread_attachments.py`, `tests/test_server_budget.py`.

**Regression — every uploaded document was TEAM KNOWLEDGE on arrival.** There
was no thread binding and no purpose, and the compiler ingested whatever it
was given straight into the team wiki, which every member can read. A file
dropped into a restricted thread would have had its contents published to
people who cannot open that thread — not by a bug in the compiler, but by the
compiler working exactly as designed on a document nobody had said was
shareable.

**Design:** three purposes, and the DEFAULT IS THE NARROW ONE. A member
dropping a file into a conversation is not publishing it, so an attachment is
`turn_context` — shown to the model for this piece of work and nothing else —
until a member promotes it. Promotion is refused permanently at the compiler
if it has not happened (nothing about waiting makes an unpromoted document
promotable) and recorded with who did it, in a table that outlives the row.

**Also:** existing thread-less documents were migrated to `team_knowledge`,
not to the safe-looking default. They were uploaded through the team screen
and compiled into the wiki, so that is what they are — calling them
`turn_context` would retroactively claim a privacy they never had, and
backdating a guarantee is a worse lie than not having had one.
**Also:** three T21 tests then went red, correctly. The column default made
EVERY new document `turn_context`, including team-screen uploads with no
thread — which would have quietly stopped the entire documents feature
compiling anything. That is how a privacy default becomes a silent outage. The
rule is about the SHAPE of the row: attached to a thread means scoped to that
thread, attached to nothing means shared with the team.

**A real flake, found and fixed on the way.** `test_server_budget._seed_runs`
defaulted to `minutes_ago=5` and the usage bucket is
`date_trunc('hour', ...)`, so for the first five minutes of every hour it
seeded spend onto the PREVIOUS hour's row — one `reserve_turn` never reads.
Confirmed directly: at 20:03, `date_trunc('hour', now() - 5 minutes)` is
19:00 while the reservation reads 20:00. A full suite run takes nine minutes
and crossed that window regularly, which is why this surfaced twice in this
session and was mistaken for contention the first time. A test that fails on
the clock is one everybody learns to re-run rather than read.

**Passing:** 11 attachment tests; full suite below.

**Migration/rollback:** additive — two columns, a purpose check, an audit
table and two triggers. The data migration is one-way in meaning (it names
what existing documents already were) but changes no behaviour on rollback.

**Ceiling:** 🔴 NO UI AT ALL. There is no composer attachment control, no
promotion button, no visibility warning shown to the member before they
promote — the model and its rules exist and nothing in the product reaches
them. 🔴 Storage authorization is unchanged: the bucket is private and read
through the service key server-side, but nothing binds a signed URL's scope to
the attachment's purpose, so a member who can call the download API for a
document id is not checked against the thread. 🔴 One unexplained failure:
`test_removing_someone_is_recorded_too` failed once in a full run and passed
in isolation and in every run since. No cause found, and recorded rather than
dismissed.

### T25 — Return GitHub CI results to originating work

**Changed:** `supabase/migrations/20260908120000_ci_correlation.sql`,
`pipeline/ci.py` (new), `server/app.py`, `shared/consent.py`,
`tests/test_ci_correlation.py`.

**Regression — a pull request Comrade opened was forgotten the moment it was
created.** `open_pull_request` returned its number to the consent flow and
nothing persisted it: no row, no branch, no thread. So when GitHub reported
that the checks had failed, there was no way to say WHOSE work had failed —
the delivery was ingested as repository activity for the wiki, and the thread
that produced the change never heard about it. A member had to go and look.

**Design:** the check result is recorded BEFORE the delivery is acknowledged.
One that is only queued is one a worker crash loses, and losing it is the
whole defect. The bookkeeping never fails the delivery, though — GitHub
retries anything that is not 2xx, and a redelivery loop is a worse outcome
than a missing row.

Matched by pull request number, falling back to BRANCH: GitHub does not always
populate `pull_requests` — a check on a fork, or one that arrives before the
PR is linked — and the branch is the other handle, the one Comrade itself
chose.

**Also:** a stale result — one about a commit the branch has moved past — is
KEPT AND MARKED rather than dropped. The failure it reports may already be
fixed, so it must not drive a decision; but an audit that quietly omits the
failures nobody acted on is not an audit.
**Also:** a check for work this team did not open is recorded with NO thread
rather than discarded. It is real repository history, and a row that cannot
say which thread is better than one that guesses.
**Also:** the existing delivery dedupe turned out to be PARTIAL rather than
absent. The job queue dedupes on `X-GitHub-Delivery`, but only while a job is
pending or processing — a manual redelivery from GitHub's UI after the first
finished would have landed twice. Closed with a unique constraint on the
delivery id.
**Also:** visibility follows the thread, consistent with T22's tasks and T24's
attachments — CI on restricted work is invisible to non-participants.

**Passing:** 13 CI-correlation tests; 106 GitHub and consent tests; 1212
backend tests, 6 skipped, 0 failed.

**Migration/rollback:** additive — two tables. Nothing existing changes shape,
and a deployment without the new code simply writes no rows.

**Ceiling:** 🔴 NOTHING SHOWS THIS TO ANYBODY. The rows are correlated,
scoped and queryable, and no thread UI reads them — the member still has to go
and look, which is the defect this task named. The data is in place for that
screen and the screen is not built. 🔴 No continuation policy: the plan asks
for at most one policy-authorised continuation per failure revision, and
nothing continues anything. That satisfies "never automatically publish a new
patch" by construction rather than by design. 🔴 No failure-summary fetch, so
no log clipping, no datamarking and no secret redaction — none of which exist
because nothing reads logs yet. 🔴 `head_sha` is recorded from what
`open_pull_request` returns; if that is absent the first check result cannot
be judged stale.

## Phase E exit gate

**1212 backend tests, 6 skipped, 0 failed. 210 frontend tests. Build green.**
T22 through T25.

The pattern here is different from Phase C's. Those were failures of ACCOUNT —
the system was right and what it said about itself was wrong. These are
failures of CONNECTION: two things that should have been one, or two things
that never met.

- The board and the thread were two answers to "is this done" (T22).
- A check and the change it checked were bound by a counter that could not
  tell them apart (T23).
- A file and the audience it was meant for were never related at all (T24).
- A failing build and the thread that broke it had no way to reach each other
  (T25).

Three of the four came with a leak that appeared the moment the connection
did: linking tasks to threads exposed restricted work on the board, attaching
files to threads published them to the wiki, and correlating CI exposed
restricted work again. Each was caught while writing the migration rather than
after, because the pattern was by then familiar — a new relationship inherits
the visibility of the WIDER side unless somebody narrows it.

**What Phase F inherits:** the same evidence gap Phase D had, plus a new one.
Four of these tasks end with "the data is right and no screen reads it". T22's
board card, T24's attachment control, T25's CI panel — the models are built
and the product cannot reach them. That is a real limit on calling any of this
done.

**A flake was found and fixed**, not worked around: `test_server_budget`
seeded usage onto the previous hour's bucket for the first five minutes of
every hour, so it failed on the clock. It had been mistaken for contention
once already this session.

### T26 — Tighten service secrets, roles, and admission budgets

**Changed:** `supabase/migrations/20260908130000_queue_payload_privacy.sql`,
`20260908140000_queue_fairness.sql`, `20260908150000_job_subject.sql`,
`shared/db.py`, `shared/config.py`,
`shared/migrations.py`, `shared/usage.py`, `pipeline/worker.py`,
`agent/runtime.py`, `pipeline/repo_sync.py`, `pipeline/repo_env.py`,
`server/github_connect.py`, `docker-compose.yml`, `scripts/deploy_host.sh`,
`.env.example`, `tests/conftest.py`, `tests/test_service_isolation.py` (new),
`tests/test_in_run_budget.py` (new), `tests/test_queue_fairness.py` (new),
`tests/test_deploy_host_script.py`, `tests/test_ingestion_minimisation.py`.

**Regression — the cross-team role could read every team's queued content.**
`comrade_control` held `select (payload)` on `jobs`, and it is the ONE role
deliberately not scoped to a team: `ctl_jobs` is `using (true)`, because a
single sweeper serves everybody. `enqueue_github_event` queues the whole parsed
webhook body, so a private repository's pull request descriptions, commit
messages and review comments sat in a column readable under one credential
that spans the deployment. T21 moved DOCUMENT bytes out of the payload for
exactly this reason and left every other payload where it was; `compiler.py`
even carries the comment "`jobs.payload` — a table `comrade_control` can read".

**Design:** the claim stops returning the payload and the worker re-reads it
under `comrade_pipeline` scoped to the job's own team, where `pl_jobs` confines
it to `current_team()`. Same content, through the role that is allowed to see
that team and only that team. The control plane keeps everything it needs to
RUN the queue — ids, status, attempts, leases — and none of what is in it.

**Regression — an unset role URL was not "unconfigured", it was "guess".**
`connect()` refused an empty URL; `team_session()`, which is the path every
worker actually uses, handed it straight to a pool. An empty conninfo makes
libpq fill in the blanks from PGHOST/PGUSER, a .pgpass file, or peer auth on
the local socket — so on a host where peer auth works, forgetting
COMRADE_CONTROL_DB_URL was a superuser session with no row security, arrived at
by omission. One `_url()` now resolves every role, and both paths go through it.

**Regression — every service carried the table owner just to boot.**
`comrade_db_url_admin` had no default, so the API and both workers had to hold
the RLS-bypassing credential in their environment to import their settings, and
`_URLS` put it one `connect(Role.ADMIN)` away. The existing guard greps runtime
source for the literal `Role.ADMIN`, which is a lint, not an enforcement.

Now off by default and turned on by a CALL — `allow_table_owner()` — rather
than by a variable, because a property of what a process IS should not be
something a deployment inherits by accident. Migrations and the test suite call
it; nothing that serves a request does. The deployment half matches: the
compose file blanks the variable for `api`, `pipeline-worker` and
`agent-worker`, and a new one-off `migrate` service (behind a profile, so
`up` never starts it) is the only thing given the real value. The table owner
now exists in one short-lived container per release instead of in three
processes that run for weeks.

**Regression — a 20-call turn had no brake, only a receipt.** Admission
reserved 6,000 tokens — measured on a TRIVIAL turn — and then let the run make
up to `agent_max_llm_calls` model calls with nothing between them checking the
cost. A sweep that reads file after file carries the whole growing context into
every call, so one admitted turn could spend several million tokens against a
500,000-per-hour cap; `finalize_usage` reconciled the truth afterwards, which
is accounting, not a brake.

The allowance is deliberately NOT the estimate: a turn that costs more than
6,000 tokens is ordinary and must not be killed for it. It is what the run
reserved plus whatever the team still has for the hour, re-read on each check
rather than cached — other turns finalize while this one runs and give budget
back, and refusing work on a stale number turns a brake into an outage. Nothing
is read at all until a turn passes its own estimate, so an ordinary turn pays
nothing for this. The stop lands on the same boundary as the cancellation check
for the same reason: the call that just happened cannot be taken back, the next
nineteen can.

**Also — `run_turn` KeyError-ed on any terminal frame it had not been taught.**
It ends with `final["run_id"]` and `final` is `{}` until a `final` frame
arrives, so it enumerated the frames that never send one. Busy was the first,
empty the second and got its own branch; CANCELLATION (T11) and the budget stop
were the third and fourth and got none. The agent worker drives turns through
`run_turn_sync`, where that exception is a crashed worker rather than a handled
outcome. Fixed once, as a set, with a truthful fallback for the fifth.

**Also — one team's backlog was every other team's outage.** The pipeline claim
was `order by created_at` across the whole queue with no per-team consideration
at all. A team that connects a busy repository queues a job per webhook
delivery and a job per document, and every one of them is older than the next
team's first job. T12 gave agent turns a per-team ceiling for exactly this
reason and left the queue whose depth is driven by an EXTERNAL event rate
first-come-first-served. Now round-robin by the team served longest ago, still
oldest-first within a team — deliveries about one repository have to be
ingested in the order they happened.

**Two readers needed a field, not the column.** `sweep_stale_checkouts` and
the connect screen's failure list both read `payload->>'repo_full_name'` as the
control role. Restoring the grant to serve them would have handed the payload
back, so the field they need became `jobs.subject` — the non-sensitive IDENTITY
of a job's target, never its content. A repository's full name is already
readable by this role through `github_repos.repo_full_name`, so it exposes
nothing new, which is the test for whether something belongs in that column;
a regression test pins it.

**Verified rather than rebuilt.** Two checklist items were already satisfied and
are now pinned by tests instead of re-implemented: a run whose
`record_reservation` was lost still charges what it cost on top of the estimate
(an overcharge, the safe direction for a cap) and never earns free budget; and
reconciliation is idempotent across all four terminal paths.

**Passing:** 11 service-isolation tests, 17 in-run budget tests, 4 queue
fairness tests, 6 deploy tests; 1243 backend tests, 6 skipped, 17 deselected,
0 failed. 210 frontend tests; build and lint green.

**Migration/rollback:** a revoke, a `subject` column with its backfill, and
two indexes. The revoke is the one thing
here that is NOT safe against old code still running — the previous worker
selects `payload` in its claim — so it must land in the same release as the new
`pipeline/worker.py`, which is what the build → migrate → activate ordering in
`deploy_host.sh` gives it. Rolling the code back without reverting the revoke
breaks job claiming.

**Ceiling:** 🔴 A HARD crash between `reserve_turn` and `enqueue_turn` still
leaks one turn and one estimate onto the team's bucket, with no run row that
will ever finalize it. Bounded and self-healing — it expires with the hour, and
it is 1/60th of the turns and 6,000 of 500,000 tokens — so it is recorded
rather than closed with a durable reservation ledger. 🔴 The in-run brake fires
BETWEEN calls, so a single call that costs more than the whole hourly budget is
paid for in full before anything can object; nothing bounds one request's
context. 🔴 The brake is per-run and does not compose. A running turn's spend
reaches the bucket only at finalization, so what a concurrent run reads is the
other run's RESERVATION, not what it has actually spent — two simultaneous
runaway turns can therefore overshoot the cap together even though neither
passes its own allowance. Closing it means charging incrementally during the
run, which is exactly the once-only reconciliation invariant this task was
asked to preserve, so it is named rather than built. 🔴 The stop is reported to the member through the run's `last_error`
on the terminal frame, which the room already renders — there is no distinct
budget affordance, no "resume next hour", and no way to raise the limit from
the product. 🔴 Per-team limits on UPLOADS are still only the per-file
`MAX_DOCUMENT_BYTES`; nothing caps how many documents or how much total volume
one team may push through the compiler. 🔴 Round-robin fairness is by last
service time, which is fair per JOB and not per unit of work — a team whose
jobs each take ten minutes still consumes more of the worker than one whose
jobs take ten seconds.

### T27 — Add truthful health, structured observability, and safe drain

**Changed:** `shared/errors.py` (new), `shared/observability.py` (new),
`supabase/migrations/20260908160000_usage_metrics_grant.sql`, `server/app.py`,
`pipeline/worker.py`, `agent/worker.py`, `agent/runtime.py`,
`agent/run_queue.py`, `agent/processes.py`, `pipeline/compiler.py`,
`shared/agent_runs.py`, `shared/config.py`, `docs/operations.md` (new),
`.env.example`, `tests/test_error_redaction.py` (new),
`tests/test_observability.py` (new), `tests/test_drain.py` (new),
`tests/test_metrics.py` (new), `tests/test_readiness.py` (extended).

**A near-miss worth recording.** `tests/test_readiness.py` already existed with
seven tests, and I wrote it as if it were new — the Write reported success and
replaced the file whole. Caught at review, when `git status` showed it as `M`
among a group I expected to be `??`, and recovered from HEAD. All seven are
restored and passing against the new route; only two assertions changed, both
because they had become factually wrong (the check set grew from three names to
six). Had that file been uncommitted work from earlier in the session it would
simply have been gone.

**Regression — the error column handed over exactly what T26 had just taken
away.** Every failure path recorded `str(exc)` verbatim. A Postgres error is
not a short sentence: it attaches `DETAIL: Failing row contains (...)`, which
is THE ROW, every column of it in order. So a constraint violation while
compiling a document or writing a message wrote that content into
`jobs.last_error` — a column `comrade_control` reads across every team — and
into a log that is shipped somewhere else again. Confirmed end to end before
fixing: the failing row landed in the column intact.

**Design:** the diagnosis survives, the data does not. The exception CLASS, the
constraint name and the statement's shape stay; the values go. Redaction is
applied at the BOUNDARY where an error becomes durable — `_finish` on the job
queue, `finish_run`, `finish_claimed_run`, `_fail_document`, the process rows —
rather than at each caller, so the callers nobody has written yet are covered
too.

The log gets a stronger guarantee than a convention: a filter on the HANDLER
runs every record — message, arguments and traceback together — through the
same function. `logger.exception` is the case that needs it, because the
traceback carries the exception's own text and that is exactly where a DSN with
a password ends up.

**Regression — readiness compared MAXIMUMS.** `applied >= newest_on_disk`, so a
release that skipped one migration in the middle passed on the strength of the
newest being present. A migration that failed and was retried, a rebase that
reordered two files, a partially restored backup: each leaves a gap below the
top, and the column nobody created is then a 500 on whichever request first
touches it — which reads as a code bug rather than a half-finished release. Now
a set difference, and the missing versions are NAMED: "3 missing" sends an
operator to diff two lists by hand at the moment they can least afford it.

**Regression — one role's connectivity stood for five.** `/ready` probed
`comrade_control`, the role the API itself answers with. A turn needs
`comrade_agent`, an approved action needs `comrade_executor`, a document needs
`comrade_pipeline` and a member read needs `comrade_authenticator` — so a
half-done credential rotation, which T26 turned into a routine operation, left
the deployment green while every turn in it failed. Probed directly rather than
through the pools: the question is whether the CREDENTIAL works, and a pool
borrow on a bad one waits out its whole timeout before saying so.

**Regression — half the product was invisible.** The pipeline queue was not
checked at all. A wedged pipeline worker means no document compiled and no
memory written, silently, while the agent queue it did check moved fine. Added
with expired leases — work a dead worker is holding that nobody is doing, which
no queue-depth check would show, because the rows are not pending.

**Regression — a stop claimed one more job.** The drain guard read
`if _stopping.is_set() and processed: break`, and `processed` is 0 at the top of
a batch. So a stop arriving while the worker was idle claimed a job anyway,
which then had the whole shutdown grace period to finish or be killed
mid-flight — and a job killed mid-flight waits out its entire 30-minute lease
before anyone redoes it. The comment above the line said the right thing; the
condition did not.

**Regression — the documented escape hatch did not exist.** Both workers'
`_drain_on_signal` docstrings said "second signal is not caught, so an operator
who means it can still kill the process outright". `signal.signal` installs a
PERSISTENT handler; it is not reset after delivery. So every subsequent SIGTERM
was swallowed exactly like the first, and a worker wedged inside a long job
could not be stopped with anything short of SIGKILL. Found while checking the
runbook's claims against the code rather than transcribing them — the first
signal now hands itself back to the default handler, which makes the sentence
true. An escape hatch that is documented and absent is worse than one never
claimed: it is what an operator reaches for when the first attempt did nothing.

**And the first version of the redactor was wrong about class names.** It
prefixed every message with the exception's type, which is right for a library
error — nobody wrote `CheckViolation`'s message for a person, and its type is
most of the diagnosis — and wrong for ours. `PermanentJobError`,
`BudgetExceeded` and their siblings are raised with a sentence somebody wrote,
and several of those strings are shown to MEMBERS: `jobs.last_error` is what
the connect screen renders when a clone fails. The full suite caught it as one
failing assertion, and the member-facing consequence was "PermanentJobError:
no GitHub credential reaches acme/app" on a setup screen. Now: a library
exception keeps its class, ours keeps its sentence.

**Also — the runbook's numbers were wrong the first time I wrote them.** I
claimed `stop_grace_period: 300s` for both workers. The pipeline worker's is
120s and its lease is 30 minutes, not 5 — so an operator sizing a deploy window
from that table would have been out by an order of magnitude, and a deploy
during a large repository sync can leave a team's checkout stale for half an
hour with nothing failing and nothing to see. Corrected, and both the check
NAMES and the recovery NUMBERS are now pinned by tests against the code and the
compose file, because a runbook that drifts is worse than none.

**Also — `/metrics`, gated closed.** Nothing counted anything, so every
operational question was a SQL query somebody had to write from memory. Queue
age, lease loss, compiler lag, run outcomes and hourly spend against the caps
are now one call. Aggregates only and no string from any row: an endpoint that
lists which team is spending what is a cross-team disclosure wearing a
monitoring hat, and one that reported `last_error` would undo the redaction
above by another route. It answers 404 unless `COMRADE_METRICS_TOKEN` is set —
"public unless somebody remembers to put a proxy in front" is the fail-open
default this codebase keeps having to remove — and the token is compared with
`compare_digest`.

**Also — the first version of the lag metric could not see the thing it is
for.** `compiler_lag_seconds` compared every message against
`max(chat_through)` across ALL teams, so one team compiling five minutes ago
made every older message everywhere look already-compiled. The team whose
pipeline had been broken for a week — the only one worth finding — was
precisely the one it reported nothing about. Now per team, then the worst of
them; a two-day backlog behind another team's fresh compilation is the
regression test.

**Passing:** 25 redaction tests, 11 observability tests, 7
drain tests, 14 metrics tests, 18 readiness tests; 1311 backend tests,
6 skipped, 17 deselected, 0 failed. 210 frontend tests; build and lint
green.

**Migration/rollback:** one grant, additive. `usage_buckets` aggregate columns
to the control role — consistent with the line T26 drew, since turn and token
COUNTS are the same class of metadata as the run statuses and job attempts that
role already reads, and the endpoint sums them so no per-team figure leaves the
process.

**Ceiling:** 🔴 SANDBOX READINESS IS NOT CHECKED, and cannot be from where the
check lives: the API deliberately has no Docker socket (granting it would turn
a request-handling bug into a host compromise), so it cannot tell whether
Docker is alive. Dead Docker still means every repository tool fails behind a
green `/ready`. It belongs on a worker-side check that reports INTO the
database, which is a different shape from anything here. 🔴 The metrics are a
snapshot, not a series — no counters, no histograms, nothing that survives a
restart, so "how often" and "how long" questions still need the database. 🔴
Tool errors, permission wait times, preview failures and denial counts from the
plan's list are not in `/metrics`: run outcomes cover the last of them
indirectly and the rest have no aggregate to read yet. 🔴 Redaction is
pattern-based, so a secret in a shape nobody anticipated passes through; the
`DETAIL:` strip is the part that is structural rather than guessed. 🔴 No alert
is wired to anything — `docs/operations.md` defines the conditions and the
first action for each, and connecting them to a pager is a deployment decision
this repository does not make.

### T28 — Rehearse recovery and document release operations

**Changed:** `scripts/backup.py` (new), `tests/test_restore_drill.py` (new),
`docs/operations.md`, `docs/deployment.md`.

**Regression — a `pg_dump` of this database is not a backup of this system.**
The only backup guidance in the repository was one line in `docs/deployment.md`
— "Maintain database backups and rehearse restoration, including recreation of
worker login roles" — and nothing implemented it or rehearsed it.

Roles are CLUSTER-level objects and `pg_dump` is DATABASE-level. Measured on
this schema: the dump references `comrade_agent`, `comrade_executor`,
`comrade_pipeline`, `comrade_control` and `comrade_authenticator` in **115**
GRANT and CREATE POLICY statements and creates **none** of them. Restored into
a fresh cluster it fails on the first grant; restored with errors ignored it
produces a database whose row-level security policies name roles that do not
exist — which is not the smaller problem, because the entire authorization
model of this product is those five roles and those policies.

The local version of the same split had already bitten this codebase:
`scripts/restore_local_roles.py` exists because `supabase db reset` drops the
roles and leaves the schema behind. Nobody had drawn the line to production.

**Design:** a backup is TWO artifacts and `create()` refuses to produce one
without the other — it reads the globals dump back and raises if any required
role is missing from it. A missing-globals backup looks complete: it is the
bigger file, and it restores without complaint into a cluster that still has
the roles. It is worthless in the one case backups exist for.

**The drill runs, rather than being described.** `tests/test_restore_drill.py`
dumps, creates a scratch database, restores into it, and then checks the thing
that actually matters: not "are the rows there" — anyone can check that — but
**is the boundary still enforced**. RLS still enabled, the policies present, a
member of one team unable to read another team's threads, a teammate unable to
read a restricted thread they are not in, and a member who still belongs able
to read their own.

Both directions, deliberately: an empty restored table satisfies every denial
check on its own. The first version of those assertions DID pass vacuously —
the module fixture dumped before any test had seeded, so the snapshot had no
rows — and the fix was to seed and create the restricted thread before taking
the backup.

**Verified by mutation.** With the restore intact a non-member counts 0 of
another team's threads; with `row level security` disabled on that one table in
the restored copy, the same query returns 2. The drill detects a restore that
loses row-level security.

**Also — the rollback register, enforced.** "No automatic downgrade of
irreversible schema" is only a rule if something checks it. Migrations here are
expand-then-contract, so the previous image runs against the migrated schema
and a rollback is "deploy the old commit"; a migration that REMOVES or NARROWS
something breaks that and the operator finds out during an incident. A test now
requires every migration that revokes a privilege from a `comrade_*` role, or
drops a column or table, to appear in a register in `docs/operations.md` —
ten of them do, and the one whose consequence was actually traced
(`queue_payload_privacy`, which breaks job claiming) has its by-hand reversal
written out.

The guard is narrow on purpose. The first version matched any `revoke`, which
flagged 33 migrations — almost all of them the `revoke all on function … from
public, anon, authenticated` hardening idiom sitting next to a `create
function`. A guard that flags 33 files is one somebody deletes within a week.

**Also — retention, export and deletion, stated rather than assumed.** Nothing
is erased: deleting a message sets `deleted_scope` and leaves the row, so
"delete" in this product means *withdraw from view*, not *destroy*, and the
text is still in every backup taken since. There is no retention window on
anything. There is no export. The only true erasure path is `on delete
cascade` from a `teams` row — 61 of them — which has no UI, which is the safe
way round. All four are written down as gaps rather than described as
features.

**Passing:** 13 restore-drill tests; full suite below.

**Migration/rollback:** none. Tooling and documentation only.

**Ceiling:** 🔴 THE DRILL RESTORES INTO THE SAME CLUSTER, so it exercises the
database half and not the roles half — the roles are already there. What it
checks about globals is that the ARTIFACT contains `CREATE ROLE` for each of
the five; a genuinely fresh cluster has not been tried. 🔴 The measured
numbers — backup 1.2s, restore 6.8s on a 1.3 MB dump — are from a near-empty
development database. They establish the method and the ratio, not a
production RTO, and the runbook says so. 🔴 Storage objects are not backed up
by any of this: `database.sql` holds the paths and Supabase Storage's own
backups hold the bytes, and the two are not taken together, so a restore can
produce documents whose files are missing. 🔴 The rollback drill is a register
and a rule, not an exercise: no previous image has actually been deployed over
a migrated schema here. 🔴 Six of the ten register entries say "consequence not
traced" — they are historical, and a consequence nobody verified is not
written down as if it had been.

## Repair checklist (fix.md)

A read-only review pinned at `7be319e` produced 34 findings across T01–T27.
Worked in the order the document itself sets, and **every finding was
re-confirmed against the current branch before it was fixed** — the document
asks for that, and one of its own items turned out to overstate the situation
while several understated it. Each fix was then mutation-checked: the new tests
were run against the old behaviour to prove they fail.

**Closed: 16 of 34.** Commits `d701f56` and `e3990eb`.

### The two that should not have shipped

**F24/F25 — T25's feature never ran once.** The webhook route passed the
delivery id as a fifth POSITIONAL argument to a keyword-only parameter, so
every check event raised `TypeError: record_check_result() takes 4 positional
arguments but 5 were given` into a broad `except` and the delivery was
acknowledged with nothing recorded. And `_create_pr` returns `pr_url` and
`created`, while the consent flow reads `result["number"]` — a KeyError into a
second broad `except`, so no pull request was ever correlated with its thread.

The lesson is in the test shape, not the typo: T25's tests called
`record_check_result(...)` and `record_pull_request(...)` directly, with the
correct keywords, and were green over both broken call sites the whole time. A
unit test of a helper says nothing about whether anything calls it correctly.
The replacements go through the signed webhook route and through the real
`_create_pr` with its transport mocked.

The review also corrected the DESIGN, not just the call. The T25 entry above
argues the result must be recorded before the delivery is acknowledged, because
"one that is only queued is one a worker crash loses". That is not true of a
queued JOB — a durable row with retries and backoff — and logging-and-
continuing was what actually lost results. Correlation now happens in the job
handler.

### The rest, by what they were

| | Defect, as reproduced |
|---|---|
| F01 | Previews OFF — the default — made `caddy:2-alpine` refuse the whole config; the release printed "deployed" over the outage because its check ran inside the api container against localhost |
| F03 | `max_size=None` let a team's own preview server exhaust the internet-facing API |
| F04 | Stream authorization was checked at connect and never again; a removed participant kept receiving private steps |
| F11 | The per-team run ceiling was advice under concurrency: measured, 2 admitted against a ceiling of 1 |
| F19 | The audit log snapshotted whole rows behind a team-wide policy, including `documents.parsed_text` |
| F20 | Member-written `storage_path` read with the service secret, with no ownership check |
| F22 | The recorded content hash was never compared with the bytes fetched |
| F26 | The verification digest hashed untracked FILENAMES, so a checked new file could be rewritten |
| F27 | A failed git measurement produced a stable digest that matched another failure |
| F31 | uvicorn configures logging before importing the app, so access logs never reached the redactor |
| F32 | PostgreSQL puts values in the PRIMARY message, not only in `DETAIL` |
| F33 | Empty queues with both workers dead read as ready |
| F34 | Compiler lag counted private, AI and deleted messages that capture never looks at |

### Two of my own fixes were wrong first

**The F31 filter wedged the server.** Redacting by mutating the record cleared
`record.args`, which uvicorn's `AccessFormatter` reads positionally. Every
request then raised inside logging, and under a captured pipe the tracebacks
filled the buffer and stopped the API answering. A privacy fix that took the
product down. Redacting the FORMATTED output instead is both safer and
formatter-agnostic.

**The F33 heartbeat exposed a latent race.** Adding a database round trip to
the worker loop widened a window in an existing drain test that asserted one
claim while the worker runs two slots — each of which legitimately claims once.
The test was pinned to one slot rather than the assertion loosened.

### Deliberately not fixed

**F02 is confirmed and open.** Measured directly: a container on an
`--internal` network reaches another member on port 8000 and gets a response,
so a preview can dial the API, which is attached to every preview network.
`--internal` blocks egress, not lateral traffic. The fix needs a per-preview
transport container — and fix.md's own F17 says to reassess F02 and F05–F08
after the Docker-versus-ASCII-Box provider decision. Building it now risks
throwing it away.

**Passing:** 1395 backend tests, 7 skipped, 17 deselected, 0 failed, in 14:56.
210 frontend tests; build and lint green.

**Ceiling:** 🔴 18 findings remain open, including every acceptance item
(A01–A12) and the whole of F05–F10, F12–F18, F21, F23, F28–F30. 🔴 The
readiness sandbox check trusts what the worker reports; a worker that lies or
whose heartbeat is stale in a way the freshness window tolerates is not
detected. 🔴 F19 scopes audit reads by thread, so once a THREAD is deleted its
audit rows are unreadable by any member — the evidence survives for an operator
with the table owner, and that is a deliberate fail-closed choice rather than a
solved problem. 🔴 The 3h10m suite run that preceded the clean one was my own
doing: running targeted tests against the same database while a full run was
live produced three failures that were pure contention, and I nearly read them
as real.

### Second pass — F12–F16, F18, F21, F23, F28–F30, F09, F10, F05–F08

**Closed: 32 of 34.** Commits `0fc854e`, `b410943`, `a7553a1`, `98207e7`,
`aa2380a`, `5d53f3d`, `181206e`, `f58602b`, `6b076a4`, `0225609`, `aac8f4e`. Same method throughout: reproduce first, implement, then
mutate the fix back and confirm the new tests go red.

#### What the reproductions actually showed

**F16 — the hole was measured, not argued.** With a summary through message 40
and a thread at 61, the replay started at message 42 and message 41 was
invisible to the turn. Compaction advances the summary in jumps of
`MIN_COMPACT_MESSAGES` (40) while the runtime replayed the last
`agent_history_turns` (20), and nothing made the two windows meet. T19 exists
because a constraint stated a hundred messages ago was lost; it fixed the far
end and left a moving hole just behind the replay window, which is where a
constraint stated ten minutes ago lives. `replay_for_turn` now composes the
summary cursor and the replay in ONE place — two callers assembling "what does
a turn see" separately is how they came to disagree.

**F09 — the composer was locked for the whole run, and the backend had
supported the alternative since T15.** `enqueue_agent_turn` answers a send made
during a live run with `disposition = 'steering'`. The room could not reach it,
because `sending` was held until `follow()` finished watching the run. Releasing
it exposed the second half: steering returns the id of the run ALREADY being
followed, so re-following would reattach at `after_seq=-1` and replay every step
card on screen. `follow` is now idempotent for the run it is on.

**F10 — only the tidy interruption reconnected.** `followRun` returns `aborted`
for a signal and `truncated` for a clean end, so everything reaching the catch
arm was the connection actually dying — the commonest interruption there is,
and the only one that never retried.

#### The sandbox batch (F05–F08)

**F05 measured 33.5 MB held for a 16 MB flood.** `run_setup` was the last
`capture_output=True`, on the one phase with the network on, running
`pip install`'s setup.py and npm's postinstall scripts for up to ten minutes.
Clipping afterwards protects the log, not the worker. Moving it to `run_bounded`
brought the peak to 3.2 MB — and took **57 seconds**, because `BoundedOutput`
fed its tail a byte at a time into a `deque(maxlen=…)` of boxed ints: about a
megabyte per stream to store 128 KB, and a loop iteration per byte. The
safeguard was its own denial of service. Chunk-wise: same bound, 5 seconds,
2 MB peak.

**F06 — four of the seven advertised recipes could not work.** The image was
`python:3.12-slim`, so `npm`, `pnpm` and `npx` were absent and all three node
recipes died on "npm: not found" inside the sync pipeline, where nobody sees
it. `npm ci --prefix /deps` does not redirect output — `--prefix` IS the
project root, so npm looked for `/deps/package.json` and found nothing, while
the manifests sat in the read-only checkout where `node_modules` could not have
been written anyway. `uv sync --project /workspace` targets
`/workspace/.venv`, the same read-only mount.

Verified against the real image rather than the argv: npm, pnpm, uv and poetry
each install a small locked project through the actual generated script, and
the run phase — network off, deps read-only, uid 10001 — imports what was
installed. That verification found something a test would not have: **`uv sync`
uninstalled uv and poetry from the venv it was executing out of**, because both
tools SYNC. uv survived as a static binary already running; poetry is pure
Python and imports as it goes, so it would have deleted its own dependencies
mid-install. The installers now live in `/deps/tools`, apart from what they
build. `poetry install --sync` also became `poetry sync`: the flag is
deprecated and slated for removal, and the installer is fetched fresh on every
setup, so its disappearance is a question of when.

**F07 — the one state the design was built to survive was the one nothing could
act on.** T06 reserves the container NAME before `docker run` precisely so a
crash before the id write-back leaves evidence. Every reclaimer then filtered on
the id: `reconcile`, `stop`, the expiry sweep, and the delete trigger alike. The
second half was a distinction never made — `_inspect_state` returned None both
for "no such container" and for "the daemon would not answer", and the caller
writes "the container is gone" for None, so a daemon restart rewrote every live
preview as failed and dropped the ids that made them findable.

**F08 — the queue that could not empty.** The drain removed each network with a
bare `docker network rm` while the preview proxy's endpoint was still attached,
which Docker refuses every time, for as long as a proxy exists. Rows therefore
failed permanently, and `order by requested_at limit 50` keeps a failing row at
the FRONT of that order — so fifty stuck rows consumed every pass and nothing
behind them was ever reclaimed. Ordering by attempts fixes the starvation; a
`next_attempt_at` backoff stops that fix becoming a hot loop.

#### Two tests were asserting defects as requirements

`test_validate_rejects_unknown_action` asserted the normalised action `== "add"`
for input the compiler could not understand, and
`test_a_revision_with_no_expectation_still_works` argued in its docstring that a
missing version "must not become an error". Both were rewritten to the contract
rather than the behaviour.

#### Two tests the batch exposed rather than broke

`test_changing_the_recipe_invalidates_every_environment` monkeypatched
`RECIPE_VERSION` to the literal `"2"` to prove a changed version changes the
key. Bumping the real version to `"2"` for the new layout made it compare a key
against itself — the assertion had been comparing two identical strings and
passing on `!=` only because the value happened not to have reached the literal
yet. It now derives the other value from the current one, so it cannot collide
again.

`test_a_command_that_will_not_stop_is_actually_killed` failed once in a full
run and passes every time alone. It counts `comrade-run-*` containers
GLOBALLY — which is the only way to see a container the caller has lost track
of, and the whole point of the test — so a sibling test's container still
shutting down reads as this one having survived. That is a race with the
neighbours, not a fact about the kill. It now waits for the count to settle;
a container that really survived loops forever, so the window cannot hide the
failure the test exists to catch.

#### One column grant was missing, and that was correct

Recording the recovered container id failed with `permission denied for table
sandbox_processes`: the control role has per-column grants, and `container_id`
was readable but not writable. Granted explicitly
(`20260908290000`) rather than widened — the role stays metadata-only, which is
why the grants are per-column at all.

**Passing:** 1516 backend tests, 7 skipped, 17 deselected, 0 failed. 224
frontend tests (33 files); typecheck and build green. Sandbox image rebuilt with Node 22,
npm 10.9.8 and pnpm 9.15.4; pytest, ruff, mypy and uid 10001 unchanged.

**Ceiling:** 🔴 **F02 and F17 remain open, and they are the same decision.**
F02 is confirmed by measurement — a container on an `--internal` network
reaches another member on port 8000, because `--internal` blocks egress and not
lateral traffic — and its fix is a per-preview transport container that F17 may
make moot. F17 asks for ASCII Box to be wired into execution and preview; that
is a product decision about which sandbox provider ships, not a defect to
repair, and fix.md itself says to reassess F02 and F05–F08 after it. F05–F08
were done anyway because Docker is the configured backend today and those paths
are not retired. 🔴 All twelve acceptance items A01–A12 remain. 🔴 F06's own
acceptance asks for the install path exercised through the real setup container,
which needs the registry egress proxy; the end-to-end runs above used the real
image and the real generated script but bypassed the proxy, so the egress
policy itself is still only unit-tested.


### A04, and the provider decision · `c8f03c6`

**A04 — an oversized preview response finished successfully.** The streaming
body counted bytes correctly and then, past the cap, did `return`. Returning
from an async generator is how a body ENDS NORMALLY: the server sends the
terminating chunk and the transfer completes, carrying the upstream's status —
usually 200. A browser asking for a JavaScript bundle past the cap therefore
received a *successfully completed* resource missing its second half, and
failed later, somewhere else, in a way nobody traces back to a size limit.

The generator's own docstring claimed the opposite — "the connection ends
without a clean close, which a browser reports as a failed load instead of
rendering half a file as though it were whole". That was the intention.
`return` is not how it is spelled. The transfer now aborts, because once the
headers are gone there is no status left to send; that is the whole difficulty
of the case, and a body that merely stops is a body that completed. A declared
content-length still gets a proper refusal with a status, before anything is
sent.

The test drives real sockets on both ends — a raw HTTP upstream, uvicorn
serving the app — because the defect is not in the counting. It is in what the
ASGI server does when the generator finishes, and an in-process TestClient
never runs that code: it hands back whatever was yielded and calls it a
response.

**One of my own tests was deleted rather than kept.** A `tracemalloc` check for
"the overflow is not buffered" measured a process running both the app and the
HTTP client, so it read httpx's client-side buffering as though it were the
proxy's — it reported 54 MB and would have gone on reporting it after any fix.
It would have looked like evidence. What replaced it counts bytes across the
socket, which is the property the cap is actually for.

**Provider decision (owner, 2026-09-09): ASCII Box.** F17 — wiring the existing
`BoxClient` into execution, dependency setup and the preview lifecycle — is
approved and is the next task. It is NOT started here: the owner also chose to
close out and hand over, and F17's acceptance cannot be satisfied from this
machine in any case. It requires provider-side evidence of Box execution in a
disposable Box, and fix.md is explicit that a locally installed `box.exe`
proves nothing about the deployed backend.

**F02 stays confirmed and unfixed, on purpose.** Measured directly: a container
on an `--internal` network reaches another member on port 8000 and gets a
response, because `--internal` blocks egress and not lateral traffic, and the
preview proxy sits on every preview network. Under Docker the fix is a
per-preview transport container — which the Box decision may make moot, which
is exactly why fix.md orders the reassessment after the provider choice.
F05–F08 were repaired regardless, because Docker is the configured backend
today and those paths are not retired.

**Gate:** `scripts/gates.sh`, commit `c8f03c6`, all lanes green — backend
1521 passed / 7 skipped / 17 deselected in 10:04; frontend build; lint (3
pre-existing fast-refresh warnings); frontend unit + component 224 in 33 files;
frontend integration 21 in 6 files; **browser journeys 8 passed**; real GitHub
2 skipped, "no GitHub credential configured".

🔴 **The first run of that gate exited 0 with a lane that never ran.** It
printed "the API on :8000 is not answering; the browser journeys need it" and
then reported success anyway. Recording it as green would have been precisely
the false pass this whole exercise is about. The numbers above are from a
re-run with `uvicorn server.app:app --port 8000` up, which is what actually
executed the eight journeys. **A06 asks for unavailable lanes to be kept
explicit; a gate that exits 0 when it skips one cannot do that, and that is
worth fixing before the gate is trusted for release.**

**Where this leaves fix.md.** Every reproduced defect is fixed except F02.
Closed: F01, F03, F04, F05–F16, F18–F34, and A04. Open: F02 (gated on the Box
boundary), F17 (approved, not started), and the acceptance programme
A01–A03 and A05–A12, which is feature and validation work rather than repair.

**Ceiling:** 🔴 A03 needs local DNS/TLS and A05 needs a disposable Linux Compose
host; neither exists here, and both remain recorded missing proofs rather than
passing lanes. 🔴 F06's own acceptance wants the install path driven through the
real setup container with its registry egress proxy. The end-to-end runs used
the real image and the real generated script — npm, pnpm, uv and poetry each
installing a locked project, then imported by the run phase with the network
off — but bypassed the proxy, so the egress policy itself is still only
unit-tested. 🔴 A01, A02, A07–A12 are untouched; several are acknowledged in
earlier entries as known limitations and remain so.


## Repair review — 2026-09-09, pinned to `c8f03c6`

A second review, of my own committed repairs. Fourteen new findings (F35–F48)
and F20 reopened. Worked in the order the owner set: F20, F40, F42–F44, then
F35–F38, then F39, F41, F45–F48. Commits `e3a04d1`, `8ff80c9`, `56c765d`,
`6fd0848`.

**Nine of the fifteen were in work I had just done.** That is the headline, and
the pattern inside it is worth more than the count: in almost every case a test
of mine passed on the broken value.

### The tests that agreed with me

**F45.** F16's own test asserted the replay was `<= 200 and >= KEEP_RECENT`
on a 300-message thread with no summary. It got 20 — inside both bounds — so
the assertion was satisfied by exactly the defect. A second test asserted
`== agent_history_turns` and called it "unchanged behaviour where there is
nothing to bridge from", which is the defect stated as a requirement. On a
fresh 59-message thread, messages 1–39 were invisible: F16 closed the gap
BETWEEN compactions and left the one before the first.

**F46.** I verified F06's node install with `require('leftpad')` and never
tried `import`. Node's ESM resolver ignores NODE_PATH entirely. Measured in a
container: `CJS: function`, `ESM: ERR_MODULE_NOT_FOUND`. Running the real thing
is not enough if you run only one of the two ways users invoke it.

**F20.** The migration I wrote claimed `shared/storage.py` "refuses a path
outside the reading team's own prefix". It did not — `download_document` took a
path and nothing else and fetched it with the service secret. A comment
asserting a guarantee that was never implemented, in the file that claims it:
the same failure I had just fixed in A04's docstring, committed four days
apart.

**F38 (my own fix, twice).** The generation name collided on seconds; I moved
to microseconds and reported it fixed; microseconds collided too, because on
Windows `datetime.now()` returns the same value for calls milliseconds apart. It
passed in isolation because the timing happened to differ. A clock is not an
identity source. The test now freezes the clock so the property is pinned
rather than left to whether time passed.

### What ON_ERROR_STOP found

**F36** looked like a missing psql flag. Turning it on revealed that this
backup had NEVER been fully restorable, and ignoring errors is what hid it.
One error at a time, against a real database: `--clean` DROPs for absent
schemas (line 30); `SET ROLE supabase_admin` (38); `realtime.list_changes`
declared `SET log_min_messages`, superuser-only (2614); ALTER DEFAULT
PRIVILEGES FOR ROLE supabase_auth_admin (11842). Then exit 0, zero errors.

The dump was of the whole Supabase database. It now carries what Comrade owns
or extends — `public`, `auth` (every policy calls `auth.uid()`), `storage`
(where T24/F18/F20 put policies) — with `--no-owner`, one named 18-line
exclusion, and an explicit `prepare_target()`. NOT `--no-acl`: the grants and
policies ARE the thing being backed up. The drill passes under ON_ERROR_STOP:
backup 0.8s, restore 2.4s, 128 public policies and 2 storage policies verified
back.

### The rest, briefly

**F37** — with no host client binaries, `_run` rewrote ANY url to
`127.0.0.1:5432` inside the local container. For a restore that is not a failed
operation, it is a successful one against the wrong database.

**F35** — the release asked caddy for `https://localhost/api/health` with
`--no-check-certificate`. Caddy serves a NAMED site, so a healthy deployment
was reported broken while a broken TLS setup passed. Now checks the configured
hostname with real SNI and verification, both upstreams, over real TLS in test.

**F40** — the verification gate opened with `if edit_generation == 0: return
False`, which counts `repo_edit` calls in this in-memory turn. A resumed turn, a
change written by a command, an untracked file, and edit→run→edit all proposed
with no verification record at all.

**F42/F43/F44** — a permission wait reset the run's recorded total; a stop
during the first model call finalized zero tokens for a response already
billed; the hour boundary refused a run that had a full unused allowance,
depending on unrelated traffic.

**F39** — the heartbeat beat from inside the work loop, so a turn longer than
STALE_SECONDS made readiness report live workers missing.

**F41** — a failed PR correlation logged that "a later attempt" would fix it;
`execute_consent` sets `executed` before the executor runs, so the retry
returns `noop` and the mapping is never written.

**F47/F48** — a failed steering send hid a live run's output and STOP button;
a rebuild kept stale staged manifests so a deleted `.npmrc` kept applying.

### Method notes

Every finding was reproduced before it was fixed and mutation-checked after —
33 mutations across the batch, each one biting. Two of those checks found my
own tests proving less than they claimed (F42's seeding and F43's ordering were
exercised only through helpers, not the turn loop), and both were rewritten to
drive `stream_turn`.

Three heredoc-escaping mistakes, all the same one: `\n` becoming a literal
newline inside a Python string. Switched to the Edit tool for anything
containing escapes.

Two test fixtures of mine leaked into shared state — `worker_heartbeats`
cleaned before each test and not after, which failed `test_readiness` in a full
run while passing alone.

**Passing:** 1609 backend, 8 skipped, 17 deselected, 0 failed. 229 frontend
across 34 files; typecheck clean.

**Ceiling:** 🔴 **F46's fix has a deployment window.** Docker refuses to start a
container whose `volume-subpath` is absent — measured — so a dependency volume
built by the old layout fails runs until the next sync rebuilds it.
RECIPE_VERSION forces the rebuild and the failure is loud rather than silent,
but it is a window. 🔴 F02 and F17 remain open together: ASCII Box is the agreed
provider, F17 is the approved next task, and F17's acceptance cannot be met
from this machine — it needs provider-side evidence of Box execution, and
fix.md says a locally installed `box.exe` proves nothing about the deployed
backend. 🔴 A01–A03 and A05–A12 are untouched; A03 needs local DNS/TLS and A05
a disposable Linux Compose host. 🔴 F06's egress policy is still only
unit-tested: the end-to-end installs used the real image and the real generated
script but bypassed the registry proxy.


## Follow-up review — 2026-09-09, pinned to `3b48448`

Six findings, every one of them in the previous two days' repairs. Commit
`6b421d1`. Each was verified here before being fixed, and two turned out worse
than reported.

### The guard whose job I did not finish

**F20, reopened a second time.** Ownership was right; what followed it was not.
The authorised name was INTERPOLATED into a URL string, and a URL is not a
path. Measured:

    '<team>/restricted.txt?owned' -> path '<team>/restricted.txt' query 'owned'
    '<team>/secret.txt%3Fx'       -> path '<team>/secret.txt?x'
    '<team>/a#b.txt'              -> path '<team>/a'

A member uploads a decoy named `<team>/restricted.txt?owned`, files its
document row, passes every ownership check on that literal name, and the
service key fetches the restricted object. Supabase permits `?` in a key, so
the decoy is a legal object and the unique-path index cannot see the collision
— the two literal names differ.

`canonical_path` rejected traversal and never named URL metacharacters. That is
the same mistake as the first F20 fix, in the same function: enumerate the
dangerous shapes, miss one. The lesson is that the guard's stated property —
*the authorised name and the fetched name are one string* — is testable
directly, and now is.

### The fix that would have broken every deployment

**F35, reopened.** The workflow pipes the release script into `sh -s <sha>`, so
`$0` is `sh` and `$(dirname "$0")/proxy_check.sh` resolved to
`/opt/comrade/proxy_check.sh` — while the committed helper is at
`scripts/proxy_check.sh`. The check I added so a healthy deployment would stop
being reported broken would have failed all of them, after activation.

Separately, `COMRADE_HOST` lives in Compose's `.env`; Compose interpolates that
for containers and exports nothing into the parent SSM shell, so a correctly
configured host reached my new unset-host failure. Both now tested through the
exact stdin invocation the workflow uses, which is the only shape either defect
exists in — calling the script by path hides both completely.

### The rest

**F36 follow-up** — narrowing the dump dropped `supabase_migrations`, so a
restored target had no record of which migrations had run. Readiness cannot
confirm the schema and the runner either fails on the missing relation or
re-applies everything over already-restored objects. The drill now asserts
every applied version comes back.

**F37, reopened** — refusing remote hosts left every loopback PORT rewritten to
5432 inside the container, and `localhost:6543` may be another database or a
tunnel to a production one. The translation is now authorised by the
container's own publication.

**F43, reopened** — the terminal-status fence stops protecting the moment the
replacement FINISHES. The stale worker then finds a terminal run and settles
its own local totals; the real owner's settlement is skipped because
`usage_finalized_at` is set, and the bucket permanently records the wrong
number. Settlement is now fenced on worker ownership, with a null `worker_id`
kept as the cancellation case.

**F48, reopened** — the environment key omitted `.npmrc`,
`npm-shrinkwrap.json` and `pnpm-workspace.yaml`, so the installer exited as
already-current BEFORE reaching the cleanup added the day before. `.npmrc` sets
the registry. The key now hashes `NODE_MANIFESTS` itself: two lists drifting
apart is how this happened.

### Two of my own tests, again

A test I added yesterday installed from the public npm registry. It failed
inside the full file at 104s and passed alone at 13s — flaky for reasons
unrelated to its subject, which teaches people to rerun until green. It now
places the dependency directly; the mount and the resolution are what it is
for, and the install script has its own unit coverage. 40 passed, three
consecutive runs, ten seconds.

And the helper measuring F20 used httpx's `raw_path`, which INCLUDES the query
— so it reassembled `name?suffix` and reported the decoy as identical, masking
the exact defect it existed to catch. Caught because `?owned` passed while
every other case failed.

**Passing:** 1645 backend, 8 skipped, 17 deselected, 0 failed. 229 frontend
across 34 files. Seven mutations across the six fixes, each biting.

**Ceiling:** 🔴 Three consecutive reviews have each found defects in the
previous round's repairs — 9 of 15, then 6 of 15. The rate is not obviously
falling, and nothing here should be read as "the fix.md work is finished". 🔴
F02 and F17 remain the same open provider decision; F17's acceptance still
cannot be met from this machine. 🔴 A01–A03 and A05–A12 untouched. 🔴 F06's
egress policy remains unit-tested only. 🔴 F46's deployment window stands: a
dependency volume built by the old layout fails runs until the next sync
rebuilds it.



## Fourth review — 2026-09-09, pinned to `bba661d`

Four findings, four commits, in the order asked for: F43, F37, the
deployment-test harness, F35.

### F43 — settlement ownership survived neither cancellation nor replacement · `c735dbc`

`finalize_usage` authorised settlement by comparing the caller's `worker_id`
against `agent_runs.worker_id`. Three paths null that column — cancellation,
lease replacement, and recovery giving up — and after any of them
`worker_id is not distinct from NULL` is TRUE for a caller passing no worker
and FALSE for the worker that was actually executing. So the stale worker that
had already lost its lease could settle the run its replacement was given, and
the replacement could not settle at all. First writer wins, `usage_finalized_at`
is set, and the hour's bucket permanently records the wrong number.

**The invariant:** the tokens a run reserved are released exactly once, by
whoever was executing when the reservation was made — and cancellation must not
change who that is.

`agent_runs.usage_owner` records it. The copy happens in a `before update`
trigger, at the moment `worker_id` is cleared, rather than in each of the three
writers that clear it and whichever fourth one gets added later:

```sql
if old.worker_id is not null and new.worker_id is null then
  new.usage_owner := old.worker_id;
end if;
```

`finalize_usage` then authorises against `worker_id` while a lease is held and
against `usage_owner` once it is gone.

```
uv run pytest tests/test_usage_continuity.py tests/test_usage_reservation.py \
              tests/test_usage_finalization.py
  ->  51 passed
```

Adjacent lifecycle, not just the reported example: the stale worker settling
first after cancellation (the report), the executing worker settling a cancelled
run, a cancelled QUEUED run settled by the server, a stranger refused on that
same row, a run recovery gave up on keeping its last owner, ordinary completion
unaffected, and settlement still happening only once.

Two pre-existing tests had setups that never established ownership at all.
Their SETUPS were fixed. Their assertions were not touched.

### F37 — the backup checked a port and called it the database · `6de127e`

`_database_endpoint` matched `docker port` output on the port number alone, so
any container publishing 54322 was accepted — a second stack, a tunnel, an
unrelated service. The address half of the mapping was parsed and discarded.
The backup then dumped whatever answered and reported success.

**The invariant:** the endpoint dumped is the one the requested address and port
actually resolve to, and an ambiguous mapping is refused rather than guessed.

`_publishes` matches the port, then requires either an exact address match or a
wildcard of the SAME FAMILY — `0.0.0.0` covers an IPv4 request, `::` covers an
IPv6 one, neither covers the other. A name (`localhost`) resolves to no single
family, so it is accepted only against a wildcard and otherwise refused as
ambiguous. The error names what the container actually publishes.

```
uv run pytest tests/test_backup_integrity.py tests/test_restore_drill.py
  ->  44 passed, 1 skipped
```

Tests use the real bare-endpoint output shape (`54322/tcp -> 0.0.0.0:54322`)
and cover a different loopback address, a different family, an exact match, both
wildcards, an ambiguous name, a port mismatch, an unpublished port, and the
prefixed shape.

🔴 One older parametrised case asserted that `[::]:54322` satisfies an IPv4
request — the port-only rule, written into a test. It was rewritten, not
weakened around.

### The harness that could test the wrong `git` · `a1dbf47`

The deployment tests prepended their double directory to the WINDOWS
environment `PATH`, separated by `;`, and then launched Git Bash. Git Bash
builds its own POSIX `PATH` at startup, and whether a `;`-separated Windows
entry survives that conversion depends on the host: on one machine the doubles
won, on another `/mingw64/bin` came first, so `git` resolved to the REAL binary
while `flock` resolved to the double.

Where it lost, the script ran a real `git fetch origin abc123`, died with
"couldn't find remote ref", and never reached the stage under test. Four
parameterised failure cases still passed — they asserted only a nonzero exit and
the absence of activation, and an unrelated early failure satisfies both. The
suite was green about a release it had not exercised.

**The invariant:** a test that reports on a stage has reached that stage.

* Precedence is established INSIDE the launched shell, in the shell's own path
  syntax (`_posix`), for both runners.
* `_assert_doubles_win` resolves each double with `command -v` BEFORE the script
  runs, so a test whose doubles are not in effect says so instead of passing.
* Every negative case names the call that PROVES it reached its own stage
  (`STAGE`), and every non-lock case asserts the release got as far as its own
  `git fetch`.

```
uv run pytest tests/test_deploy_host_script.py   ->  15 passed   (at a1dbf47)
```

Mutation-checked in two directions: with the precedence work removed the guard
test fails as designed; with the stage proofs removed the failure cases pass on
an unrelated early exit, which is the defect itself, reproduced.

🔴 **LIMITATION.** The underlying PATH conversion cannot be reproduced on this
host — here the Windows-`PATH` form happens to win. What is verified locally is
that the guards fire when the mechanism is removed. That this host reproduces
the host that failed is NOT established, and no amount of local green will
establish it.

### F35 — a second `.env` parser, and a precedence I had assumed · `417bb54`

The public-path probe needs the configured hostname. Compose interpolates
`.env` for the containers and exports nothing into the parent SSM shell, so a
correctly configured host reached the unset-host failure. The fix for THAT read
`.env` with `sed` — but `.env` is Compose's format, not a shell's. Given

```
COMRADE_HOST=comrade.example.test # public hostname
```

Compose configures `comrade.example.test` and the sed produced
`comrade.example.test # public hostname`, which the probe then asked for.
Quoting, surrounding whitespace and `${VAR}` interpolation diverge the same way.
This check runs AFTER activation, so the release reports failure over a working
deployment.

**The invariant:** the hostname the probe asks for is the hostname the
containers were configured with. One resolver, and it is Compose's.

```sh
COMRADE_HOST=$($COMPOSE config 2>/dev/null \
  | awk -F':[[:space:]]*' '/^[[:space:]]+COMRADE_HOST:/ {print $2; exit}' \
  | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
```

🔴 **RETRACTED — this measurement was of the wrong service.** What stood here
was: "MEASURED, not assumed — and I had it backwards. On Compose 2.39.4 `.env`
wins over an exported variable." That is false, and the fifth review caught it.

```
no override:   COMRADE_HOST: from-dotenv.test
with override: COMRADE_HOST: from-dotenv.test     <- the API's env_file copy
shell only:    COMRADE_HOST: from-shell.test
neither:       COMRADE_HOST: ""
```

The second line is the api service, which takes COMRADE_HOST through
`env_file: [.env]` — the literal file text, which no shell variable can affect.
Caddy, the service that actually serves the site, is given the interpolated
`${COMRADE_HOST}` and resolved to `from-shell.test` throughout. Ordinary
interpolation precedence holds. I read the first matching key out of a
multi-service document and reported it as Compose's behaviour.

The short-circuit removed on the strength of that claim was removed for a bad
reason, though it stays removed for a good one: the release now asks the caddy
container directly and has no precedence of its own to apply. See the fifth
review below.

```
sh -n scripts/deploy_host.sh                     ->  syntax OK
uv run pytest tests/test_deploy_host_script.py   ->  23 passed
```

Six `.env` shapes — plain, inline comment, both quotings, trailing whitespace,
`${VAR}` interpolation — are run through REAL Compose and through the committed
extraction lifted out of the script, and required to agree. The extraction is
read out of `scripts/deploy_host.sh` rather than restated, so the test cannot
drift into checking a copy.

Mutation-checked: restoring the sed parser fails 13 tests; putting the
short-circuit back fails exactly the one precedence test.

🔴 A first version of that lift anchored on the assignment line, which made it
blind to anything inserted IN FRONT of the assignment — so the short-circuit
mutation was being caught by a syntax error rather than by behaviour. The lift
is now bounded by the section, not the statement.

### What each of the four actually is

| Finding | Implemented | Verified locally | Deployment acceptance |
|---|---|---|---|
| F43 settlement ownership | yes | yes — 51 tests against real Postgres, 7 lifecycle cases, mutation-checked | migration `20260909130000` has not run outside local |
| F37 endpoint verification | yes | yes — 44 tests; wildcard/family logic exercised from real `docker port` output shapes | never run against the pilot host's actual mapping |
| Harness precedence | yes | partly — guards proven to bite by mutation | 🔴 the failing host is not reproducible here |
| F35 hostname from Compose | 🔴 NO — reopened by the fifth review; the value read was the API's, not Caddy's | the six shapes passed against real Compose and proved nothing, because the assertion repeated the implementation's own wrong lookup | 🔴 no deploy has run with this |

### The gate

🔴 **RETRACTED — `scripts/gates.sh` did not exit 0, and I never measured it.**
What stood here was "`scripts/gates.sh`, exit 0 … one lane did NOT run: the
browser journeys … `gates.sh` says so and still exits 0."

The invocation was:

```
bash scripts/gates.sh 2>&1 | tail -45
```

A pipeline's status is its LAST command's, so the 0 reported was `tail`'s. The
gate itself reaches `scripts/gates.sh:125-135`, finds no API on :8000, prints
the message I quoted, and **exits 1** — `QUICK=0` by default and `--quick` was
not passed. The message I saw was the failure, not a skip. Demonstrated:

```
sh -c 'false | tail -1; echo $?'                  ->  0
sh -c 'set -o pipefail; false | tail -1; echo $?' ->  1
```

That is the exact defect `scripts/gates.sh` opens by describing — "`uv run
pytest -q | tail -3 && git commit` does not gate on anything" — committed by
the person who had just read it. No full gate pass is established for this
round; what IS established is the lane below, which was run on its own and
whose exit status was captured directly.

```
uv run pytest -q     ->  1672 passed, 8 skipped, 17 deselected  (11m35s)
npm run build                                                    ok
npm run lint                          ok, 3 fast-refresh warnings
npm run test               ->  229 passed, 34 files
npm run test:integration   ->   21 passed,  6 files
```

Up from 1645 backend last round; the 27 are this round's F43, F37, harness and
F35 tests. Those per-lane results were read from the output and are accurate;
the aggregate claim built on them was not.

🔴 The browser journeys did not run, and the standing ceilings already record
that gap — no browser-level evidence anywhere in Phase B. None of the four fixes
is claimed on it.

🔴 The realtime integration test failed its first trial ("no realtime event
within 15000ms") and passed the retry the harness makes for exactly that reason.
Unrelated to this round, but it is the flake the ceilings warn about and it is
still there.

### Method notes

Each fix was reproduced through the real path before it was written, and each
regression test was run against the unfixed code first. The F43 and F37 tests
drive the real functions against real Postgres and real `docker port` output;
the F35 shape tests drive real Compose. None of the four is closed on a mock.

🔴 What is NOT established for any of them is deployment acceptance. Nothing
here has been deployed, and the release script in particular is verified against
command doubles plus one real-Compose comparison — which is a different claim
from "the next deploy works".


## Fifth review — 2026-09-09, pinned to `f474b48`

Two of the previous round's four repairs were incomplete, one accounting
lifecycle was missing, and one of my validation claims was false. The reviewer
independently confirmed the Windows harness fix on the host that reproduced the
original problem: 53 passed, 1 skipped.

### F35 reopened — the probe read the API's hostname · `a63488f`

The previous fix asked `$COMPOSE config` and took the first matching
COMRADE_HOST key. Every service with `env_file: [.env]` carries one — api,
agent-worker, pipeline-worker — holding the LITERAL file value. Only caddy is
given the interpolated `${COMRADE_HOST}`, and caddy is the service serving the
site. Compose prints services alphabetically, so the first match was
agent-worker's.

Reproduced with real Compose against copies of both committed files, `.env`
`COMRADE_HOST=from-dotenv.test` and shell `COMRADE_HOST=from-shell.test`:

```
services.agent-worker.environment.COMRADE_HOST:  from-dotenv.test
services.api.environment.COMRADE_HOST:           from-dotenv.test
services.caddy.environment.COMRADE_HOST:         from-shell.test   <- the site
what the release extracted:                      from-dotenv.test
```

So the probe asked for a name Caddy has no site for: the original F35 defect,
put back by its own fix, in a check that runs after activation. And the test I
wrote to accept the fix performed the same first-key search, so it agreed with
the implementation instead of testing it.

**The invariant was never the problem** — "the probe asks for the name Caddy is
serving" — and no lookup was going to satisfy it. The release now reads
`$COMPOSE exec -T caddy printenv COMRADE_HOST`. `docker/Caddyfile`'s site
address is `{$COMRADE_HOST}`, substituted from the container's environment when
Caddy loads its config, so the running container's variable IS the name the site
is served under. This step already runs after activation, so the container is
there to ask. No parse at all, which is the third and last version of "stop
writing parsers for other people's formats" in this finding.

```
sh -n scripts/deploy_host.sh                    ->  syntax OK
pytest tests/test_deploy_host_script.py         ->  24 passed
docker run --rm -e COMRADE_HOST=x caddy:2-alpine printenv COMRADE_HOST  ->  x
```

Acceptance: caddy's exact value under conflicting `.env` and shell values from
real Compose; that value reaching the probe through the complete stdin
invocation, with the `.env` value and the model's first value both present as
decoys and asserted absent; the six quoting/comment/interpolation shapes kept,
now pinned on caddy's resolved value. Mutation-checked: the first-key scrape
fails 6 tests, and so does a scrape narrowed to caddy's own section of the
model.

🔴 **The precedence claim in the fourth review is retracted above.** Ordinary
Compose interpolation precedence holds; the shell does win. I had measured the
api's `env_file` copy and reported it as Compose's behaviour.

### F37 reopened — a one-family wildcard does not cover localhost · `023cae1`

Literal-address matching was fixed last round. The NAME branch still read
`any(address in _WILDCARD for address in matching)`: any matching-port wildcard,
whatever family it answered for. A container publishing IPv4 `0.0.0.0:54322`
satisfied a request for `localhost:54322` — while `localhost` resolves `::1`
FIRST on this host, so a client following the resolver reaches an IPv6 listener
that may be a separate database or a tunnel, and the fallback then runs
`docker exec` inside the IPv4 container. For a restore that is not a failed
operation, it is a successful one against the wrong server.

Measured with publication mocked to `0.0.0.0:54322`, no connection attempted:

```
_publishes('localhost',  '54322') -> True
_publishes('::1',        '54322') -> False    <- what localhost reaches first
_publishes('127.0.0.1',  '54322') -> True
```

**The invariant:** every address the request can reach belongs to this
container. Not one of them.

`_resolves_to` asks the resolver what the host really reaches — a literal
resolves to itself, leaving exact-address matching unchanged; a name resolves to
everything behind it — and `_publishes` requires ALL of them to be covered by an
exact match or a same-family wildcard. One uncovered family refuses, naming the
addresses the request reaches instead of saying "ambiguous" and leaving the
operator to work out which half is missing.

```
pytest tests/test_backup_integrity.py tests/test_restore_drill.py
  ->  47 passed, 1 skipped
```

One existing test published IPv4 only while asking for `localhost` and now
refuses; its SETUP was fixed, not its assertion — it is about what the fallback
preserves, so it gets an unambiguous endpoint. Acceptance covers the dual-family
refusal, the dual-family acceptance, and a test that takes whatever `localhost`
resolves to on the host running it, requires publishing exactly that set to be
accepted and dropping any member to be refused — so it pins the rule rather than
this machine. Mutation-checked: the one-family branch fails 2, `all` weakened to
`any` fails 3.

### F49 — a cancelled parked run kept its tokens reserved · `87d2029`

A permission wait checkpoints what the run has spent, drops the lease and
returns from the runtime. No worker is left. The cancellation route settled only
runs it found `queued`, so cancelling from `waiting_for_permission` set a
terminal status and settled nothing — and nothing else ever would. A
6,000-token reservation stayed charged for the rest of the hour after a turn
that spent 1,200 was stopped.

`usage_owner`, added for F43, correctly preserves who WAS executing. That is
what stops a stale worker settling someone else's run. It does not conjure a
caller: **identity is not a settlement.**

**The invariant:** a run that reaches a terminal state releases its reservation
exactly once — including when there is no worker left to do it.

`settle_from_checkpoint` takes no identity because it takes no totals either:
the amount is the checkpoint on the run's own row, written by
`pause_for_permission` in the same statement that parked it. It is fenced on a
terminal status AND `worker_id is null` — together, nobody is executing, which
is the only condition under which a caller that did not do the work may account
for it.

`cancel_run` returns the status it cancelled FROM, from the same locking CTE
that does the cancelling. Reading the status first and acting on it after is a
different transaction from the write, so a claim or a resume landing in between
would have the server settle a run somebody had just picked up.

This also fixed the resumed-queued case: approval requeues a parked run, and
settling `queued` as zero would refund budget an earlier segment really spent.
The checkpoint says what it spent, so both cases become the same rule.

```
pytest tests/test_run_cancellation.py tests/test_usage_continuity.py
       tests/test_usage_reservations.py tests/test_agent_resume.py
       tests/test_agent_run_queue.py tests/test_queue_fairness.py
       tests/test_run_stream_resume.py            ->  75 passed
```

Acceptance runs the real HTTP route against real Postgres: park through
`pause_for_permission`, cancel, and the reservation reconciles to actual usage
exactly once; cancelling twice does not refund twice; a resumed run cancelled
before its next claim keeps what it spent; a run that never executed still
settles nothing; an executing run is still left to its own worker.

🔴 Mutation-checking found the lease fence had no test — every caller reaches it
through `cancel_run`, which nulls `worker_id` on the way past, so no caller can
present the state it guards. But `finish_run` sets a terminal status WITHOUT
clearing `worker_id`, so an ordinary completed run is terminal and still held.
That state is now reached directly, and the fence bites: settling only `queued`
fails 2, settling zero instead of the checkpoint fails 3, dropping the fence
fails 1.

### The gate, measured this time

Run twice, with the exit status captured into a variable immediately after
`scripts/gates.sh` rather than inferred from a pipeline or a compound command.

**First run, as the previous round left the host — `GATE EXIT: 1`.** The gate
reaches the API check at `scripts/gates.sh:125-135`, finds nothing on :8000 and
exits 1. This is what the previous round reported as "exit 0 with the browser
journeys skipped".

```
backend      1682 passed, 8 skipped, 17 deselected   (12m02s)
frontend     229 passed / 34 files;  integration 21 passed / 6 files
browser      DID NOT RUN — gate exited 1 here
```

**Second run, after starting the API the gate asks for — `GATE EXIT: 0`.**

```
uv run uvicorn server.app:app --port 8000
curl -fsS localhost:8000/health   ->  {"status":"ok","database":"ok"}

=== backend (pytest) ===        1682 passed, 8 skipped, 17 deselected (11m50s)
=== frontend build ===          ok
=== frontend lint ===           ok, 3 fast-refresh warnings
=== frontend unit + component ===   229 passed / 34 files
=== frontend integration ===        21 passed / 6 files
=== browser journeys (playwright) ===    8 passed (40.6s)
=== real GitHub end to end ===      2 skipped, 1705 deselected
all gates passed                GATE EXIT: 0
```

Backend is 1682, up from 1672: the ten new F35, F37 and F49 tests. The backend
lane held the database alone both times.

🔴 The real-GitHub lane SKIPPED — it is written to skip cleanly when no
credential is configured, and none is here. "all gates passed" includes a lane
that did not run, and that is a property of the script, not evidence.

🔴 The realtime integration test again failed its first trial ("no realtime
event within 15000ms") and passed the retry. Same flake, still unresolved,
recorded rather than treated as noise.

🔴 A third variant of the same mistake showed up while measuring this. The first
careful re-run ended `... ; echo "GATE EXIT: $?" | tee -a log`, so the HARNESS
reported exit 0 — the status of `tee` — while the captured `$?` said 1. The
second run ends `exit $status` so both agree. Reading an exit code is not free
just because you know the trap exists.

### What each of the three is

| Finding | Implemented | Verified locally | Deployment acceptance |
|---|---|---|---|
| F35 caddy's hostname | yes | yes — real Compose for the resolution, the real caddy image for the read-back, the complete stdin invocation for the probe | 🔴 no deploy has run with this, and the `exec` read is only exercised against a double end to end |
| F37 full candidate set | yes | yes — real resolver, real `docker port` output shapes | never run against the pilot host's actual mapping |
| F49 parked settlement | 🔴 PARTIAL at this commit — two failure paths remained; see the sixth review | the five cases passed and did not cover a lease-recovered run or a failed settlement | not exercised against a real permission wait driven by a live worker |


## Sixth review — 2026-09-09, pinned to `87d2029`

F35 and F37 were accepted; the reviewer independently ran
`tests/test_backup_integrity.py tests/test_deploy_host_script.py` and got 57
passed, 1 skipped, including the real-Compose and real-Caddy-image checks. F49
had two remaining failure paths, both P-rated, both real.

My own gate at the fifth review did not cover either of them: it ran against
this same implementation and passed. A green suite is evidence about the paths
it exercises and nothing else.

### F49 follow-up 1 — a recovered run's stale checkpoint · `b205193`

`queued` does not mean the previous execution finished.
`recover_expired_agent_runs` requeues an expired run and clears `worker_id`
while the old worker may still have an in-flight model response — paid for, and
checkpointed nowhere. Cancelling in that interval found a terminal run with no
worker and settled the stale totals, often zero. When the response arrived the
old worker's exit settlement lost to the `usage_finalized_at` already written,
and the tokens were discarded.

Reproduced through the real path — enqueue, claim, reserve 6,000, expire the
lease, `recover_expired_runs()`, cancel over HTTP:

```
after recovery:  status=queued  worker_id=None  usage_owner='old-worker'
after cancel:    usage_finalized_at set, bucket settled to 0
the paid 1,200 then had nowhere to go
```

**The invariant I had written was too weak.** "Nobody is executing" is not the
same question as "this row's record of what the run spent is complete".
`pause_for_permission` writes the totals in the statement that drops the lease,
so a parked run's record IS complete; lease recovery promises nothing. Nothing
distinguished them.

`usage_checkpoint_at` (migration `20260909160000`) records the difference: set
by `pause_for_permission`, cleared by a trigger whenever a worker takes the run,
because whoever is executing may have spent more than the row knows. Settlement
requires that marker, or `usage_owner is null` — nothing ever executed, so zero
is the true number rather than a stale one.

A lease-recovered run has neither, so its reservation is HELD until the worker
that spent the tokens accounts for them. Holding costs the team admission for
the rest of that hour; releasing loses the record of real spend. For a cap,
holding is the safe direction.

The trigger pair now reads as one rule: `keep_usage_owner` remembers WHO ran it
when execution is revoked, `expire_usage_checkpoint` forgets WHAT the row knows
when execution begins.

### F49 follow-up 2 — a failed settlement stranded the reservation · `b205193`

`cancel_run` committed before `settle_from_checkpoint` opened its transaction.
A settlement that failed left the run durably cancelled and still reserved — and
the retry made it worse, because `cancel_run` then found nothing to cancel,
returned None, and the route skipped settlement entirely. No worker remains for
a parked run, so nothing else was coming.

The settlement rides in `cancel_run`'s own transaction now: cancelling a run and
accounting for what it spent are one change to the world or neither. The route's
remaining call is a repair path for a row something else left stranded, and it
is safe to attempt on every retry because eligibility lives in the statement —
a still-executing or lease-recovered run is refused.

```
pytest tests/test_run_cancellation.py tests/test_usage_continuity.py
       tests/test_usage_reservations.py tests/test_agent_resume.py
       tests/test_agent_run_queue.py tests/test_queue_fairness.py
       tests/test_run_stream_resume.py tests/test_permission_continuation.py
       tests/test_worker_concurrency.py        ->  96 passed
```

Acceptance for both, through the real HTTP route against real Postgres: recover
a lease with usage unaccounted for, cancel before a replacement claims, and the
reservation is held — then the old worker's 1,200 lands and the hour records it;
a settlement failure rolls the cancellation back with it, so the retry has
something to do and reconciles exactly once; a run left cancelled-but-unsettled
by anything else is repaired by a retry, and that same retry refuses a
lease-recovered run.

Mutation-checked: dropping the completeness clause fails 5, moving settlement
back outside the transaction fails 2, never marking the parked checkpoint fails
5.

### The gate

Status captured into a variable immediately after the script, with the API the
gate requires already answering.

```
curl -fsS localhost:8000/health   ->  {"status":"ok","database":"ok"}

=== backend (pytest) ===        1686 passed, 8 skipped, 17 deselected (11m46s)
=== frontend build ===          ok
=== frontend lint ===           ok, 3 fast-refresh warnings
=== frontend unit + component ===   229 passed / 34 files
=== frontend integration ===        21 passed / 6 files
=== browser journeys (playwright) ===    8 passed (40.8s)
=== real GitHub end to end ===      2 skipped — no credential configured
all gates passed                GATE EXIT: 0
```

1686, up from 1682: the four reproductions above. The backend lane had the
database to itself.

🔴 This is the same gate that passed at the fifth review against the
implementation this round had to fix. It exercised neither of these paths, and
passing says nothing about the ones it does not reach. The realtime integration
test again failed its first trial and passed the retry — third round running.

### What F49 is now

| | Implemented | Verified locally | Deployment acceptance |
|---|---|---|---|
| parked cancellation | yes | yes | pending |
| approval-requeued cancellation | yes | yes | pending |
| lease-recovered cancellation | yes | yes — real recovery, real route | pending |
| failed-settlement retry | yes | yes — fault injected at the real seam | pending |

🔴 **The migration has not been applied from empty.** It is re-appliable, and
both triggers were exercised against the real schema — a claim invalidates the
checkpoint, revocation preserves the owner — but the from-empty lane is
`gates.sh --with-reset`, which rebuilds the developer's database, and it was not
run. That is the one piece of this change with no from-scratch evidence.

🔴 **A process killed between the model response and the worker's settlement
still leaves the reservation held** for the rest of the hour. That is now the
deliberate choice rather than an accident — the alternative discards paid
tokens — but it is a ceiling, not a solved problem: nothing sweeps a run whose
worker never returns.


## Seventh review — 2026-09-09, pinned to `61f7fc0`

Both sixth-review F49 paths were accepted; the reviewer independently ran the
four PostgreSQL-backed suites and got 58 passed. One upgrade-state gap
remained.

### F50 — an unbackfilled legacy row is not proof nothing ran · `54fa8ce`

Both settlement columns are nullable and deliberately unbackfilled, so a run
that executed and lost its lease BEFORE those migrations carries
`attempts > 0`, `worker_id NULL`, `usage_owner NULL` and no checkpoint marker.
The eligibility predicate accepted `usage_owner is null` as "nothing ever
executed this run, so zero is the true number". On such a row it means only
"nobody has written this column yet", and cancelling one finalized its stale
zero and released the entire reservation.

🔴 **This is the same mistake as the bug directly above it in the same
predicate, one layer further back.** The sixth review found terminal-plus-
unleased being read as "the record is complete". This is a NULL column being
read as "nothing happened". Twice in the same statement, absence of evidence
was taken for evidence — and the second time I wrote the migration comment
promising unknown state would be held, then wrote a predicate that released it.

**The invariant:** "this run never executed" needs positive durable evidence,
and a column added yesterday cannot testify about last week.

`attempts` can. It is `not null default 0`, incremented by
`claim_next_agent_run` — the only path that starts execution, in all three of
its versions — and has said the same thing about every row since the queue
existed. The owner check stays beside it and is not decorative: mutation shows
two existing F43 tests depend on that half, because their fixtures produce a
run with `attempts = 0` that a worker owns.

```
pytest tests/test_run_cancellation.py tests/test_usage_continuity.py
       tests/test_usage_reservations.py tests/test_agent_resume.py
       tests/test_agent_run_queue.py tests/test_queue_fairness.py
       tests/test_run_stream_resume.py tests/test_permission_continuation.py
       tests/test_worker_concurrency.py        ->  99 passed
```

Acceptance is the sequence the finding asks for rather than a hand-shaped row.
A fixture really drops both columns and both triggers, the run really executes
and is really lease-recovered in that pre-upgrade schema, both migration files
are really re-applied, and only then is it cancelled through the API. The row
reads `(attempts=1, worker_id=None, usage_owner=None, usage_checkpoint_at=None)`
— the migration's NULLs, not the test's — and its 6,000 stays reserved. A legacy
run that was never claimed is still refunded in full through the same sequence.

Building that fixture from what I believed the migrations leave behind would
have agreed with the belief under test, which is the whole shape of this
finding.

Mutation-checked: restoring `usage_owner is null` fails exactly the legacy test,
dropping the owner half fails 3, `attempts >= 0` fails 7.

A note is appended to `20260909130000` because the reasoning recorded there is
what invited this. It is correct about `finalize_usage`, which it was written
for, and was wrong as the general statement a later predicate borrowed from it.

### The gate

```
curl -fsS localhost:8000/health   ->  {"status":"ok","database":"ok"}

=== backend (pytest) ===        1689 passed, 8 skipped, 17 deselected (11m41s)
=== frontend build ===          ok
=== frontend lint ===           ok, 3 fast-refresh warnings
=== frontend unit + component ===   229 passed / 34 files
=== frontend integration ===        21 passed / 6 files
=== browser journeys (playwright) ===    8 passed (35.6s)
=== real GitHub end to end ===      2 skipped — no credential configured
all gates passed                GATE EXIT: 0
```

1689, up from 1686: the three F50 tests. The backend lane had the database to
itself.

The F50 fixture drops two columns and re-applies the migrations that create
them, so the schema the rest of the suite then runs against is rebuilt by the
same files rather than restored from a copy. Measured, because "the same" was
worth checking rather than assuming: the definitions match, and the two columns
end up at ordinal 58 and 59 of a 25-column table, since a drop/add cycle leaves
gaps in `attnum`. Nothing reads this table positionally — there is no
`select *` on `agent_runs` anywhere — so that difference is invisible, but it
is a difference, and "identical" would have been the wrong word.

🔴 Third round in which this gate passed against an implementation the next
review found a defect in. That is not an argument against running it; it is the
measure of what it covers. Every F49/F50 defect so far has been in a path the
suite did not exercise until the review named it.

### What the settlement rule is now

A reservation is reconciled from the run's own record only when nobody is
executing it AND the record is known to be complete:

| Row | Settled by | Why |
|---|---|---|
| never claimed (`attempts = 0`, no owner) | the server, as zero | zero is the true number, on positive evidence |
| parked, checkpoint marked | the server, from the checkpoint | written in the statement that dropped the lease |
| resumed then cancelled before reclaim | the server, from the checkpoint | no claim has invalidated it |
| executing | its own worker | the row does not know what it has spent |
| lease-recovered | its own worker, later | usage in flight, nothing checkpointed |
| legacy, executed, unbackfilled | its own worker, later | unknown is not zero |

🔴 Unchanged ceilings: a worker killed between its model response and its
settlement leaves the reservation held for the rest of that hour, and nothing
sweeps it. The migrations have still not been applied FROM EMPTY — that lane
rebuilds the local database and has not been run.

---

## Standing ceilings

1. **No browser-level evidence anywhere in Phase B.** T02's origin sentinel,
   T03's cross-container reachability, and T04's real-app behaviour all need
   infrastructure local development does not have. Per the plan, previews stay
   disabled where the domain is unset rather than implying production is fixed.
2. **codex's `test_deploy_host_script.py` edits were swept into `0cd9411`** by a
   `git add -A`. Preserved, but committed on this branch rather than left in
   their working tree.
3. `npm run lint` passes with warnings.
4. **The backend suite owns the database while it runs.** `seeded` deletes and
   re-seeds the fixture team per test, so a second pytest process against the
   same Postgres wrecks whichever tests overlap. Two "failures" during T11 were
   exactly this — a file run started while the full suite was still going — and
   they did not reproduce once the suite had the database to itself. Read a
   failure from a run that had exclusive access before believing it.
