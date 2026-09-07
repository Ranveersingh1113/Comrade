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
