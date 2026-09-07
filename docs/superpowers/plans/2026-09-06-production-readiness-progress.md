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
