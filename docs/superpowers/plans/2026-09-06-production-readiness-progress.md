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
