# Operations

What to watch, what it means, and what to do about it. Every signal here is
something the system reports about itself — nothing in this document requires
an external agent to be installed.

## The two endpoints, and why they are two

| | `/health` | `/ready` |
|---|---|---|
| Question | is this process alive and able to reach Postgres | can this deployment serve a turn |
| Wire to | container restart policy, Kubernetes `livenessProbe` | load balancer, `readinessProbe`, the deploy check |
| Fails when | the process cannot reach the database at all | any dependency, migration, role or queue check fails |

Conflating them is how a deploy goes green while nothing works. It is also how
a stalled queue turns into a restart loop: restarting the API does not drain a
queue, so a stall must never reach a liveness probe.

**Docker Compose health status does not restart anything.** `healthcheck:` in
`docker-compose.yml` marks a container unhealthy and stops there — Compose has
no restart-on-unhealthy behaviour, and `restart: unless-stopped` reacts to the
process EXITING, not to the healthcheck failing. A deployment that expects an
unhealthy container to be replaced needs an orchestrator that does that
(Kubernetes, Nomad, Swarm) or an external watchdog. Comrade's compose files are
honest about this by not pretending otherwise; treat the healthcheck as a
signal for a human or a monitor to act on.

## `/ready` checks, and what each one means

`/ready` returns `{"status": ..., "checks": {...}}`. Each check reports its own
verdict, because a single boolean tells an operator that something is wrong and
nothing about which thing.

### `database`

The control-plane role cannot reach Postgres.

**Do:** check the database is up and reachable from the API container. If the
database is fine, this is a pool or a network problem — `/health` will be
failing too, and a restart is a reasonable first move.

### `roles`

One of the five runtime credentials cannot connect. The message names which:
`agent`, `executor`, `pipeline`, `control` or `authenticator`.

**This is the check that catches a half-finished secret rotation.** Each role
serves a different part of the product, so a deployment can be perfectly
healthy on the role the API answers with and unable to run a single turn.

**Do:** the named role's `COMRADE_*_DB_URL` is wrong, or its password was
rotated in Postgres and not in the environment. Fix the variable and restart
the affected service. The message never contains the password — see
`shared/errors.py`.

### `migrations`

Migrations the code carries that the database has not applied, **named**.

This is a SET comparison, not a comparison of the newest version: a release
that skipped one in the middle — a migration that failed and was retried, a
rebase that reordered two files, a partially restored backup — leaves a gap
below the top, and the column nobody created is then a 500 on whichever
request first touches it.

**Do:** run the migration job.

```bash
docker compose run --rm --no-deps -T migrate
```

That service is the only one holding the table owner. See
`scripts/deploy_host.sh`, which runs it between build and activate.

### `agent_queue`

Runs sitting in `queued` for longer than `READY_STALL_MINUTES` (10).

**Do:** the agent worker is down, wedged, or cannot reach the database. Check
`docker compose logs agent-worker`. Filter by `service=agent-worker`; a slot
that dies logs `worker slot ... failed`.

### `pipeline_queue`

Jobs pending past the same window. Documents are not being parsed, memory is
not being compiled, repositories are not being synced.

**Do:** as above, for `pipeline-worker`. This one is easy to miss because the
product keeps answering turns while it is broken — the agent queue is separate.

### `expired_leases`

Jobs still marked `processing` whose lease expired more than
`READY_STALL_MINUTES` ago. Work a dead worker is holding that nobody is doing.

The recovery sweeps are supposed to reclaim these. A pile of them means the
sweep itself is not running, which no queue-depth check would show.

**Do:** check that a pipeline worker is alive at all. Rows recover on their own
once a worker resumes; nothing needs to be edited by hand.

## `/metrics`

`/ready` answers "is it broken". `/metrics` answers the questions an operator
actually acts on: is the token cap set too tight, is the compiler falling
behind, are turns ending badly and in which way.

**It is off unless `COMRADE_METRICS_TOKEN` is set**, and returns 404 when it is
not — an endpoint that is not turned on should not advertise that it exists.
With a token configured:

```bash
curl -H "Authorization: Bearer $COMRADE_METRICS_TOKEN" https://your-host/metrics
```

```json
{
  "pipeline": {"pending": 0, "processing": 1, "failed": 2,
               "oldest_pending_seconds": 0, "leases_expired": 0,
               "max_attempts_seen": 1, "compiler_lag_seconds": 240},
  "agent":    {"runs_by_status": {"done": 118, "failed": 3,
                                  "waiting_for_permission": 1}},
  "usage":    {"turns_this_hour": 22, "tokens_this_hour": 148000,
               "teams_active_this_hour": 3, "turns_cap": 60,
               "tokens_cap": 500000, "turn_estimate": 6000}
}
```

Aggregates only, and no string from any row. No team is named, and no
`last_error` appears — reporting one here would undo the redaction below by
another route.

What to read it for:

* **`oldest_pending_seconds`** is the number `/ready`'s stall threshold is
  compared against. Watch it before choosing that threshold.
* **`leases_expired`** rising is a worker being killed — OOM is the usual
  cause, and nothing else reports it.
* **`compiler_lag_seconds`** is how far behind the wiki is from the
  conversation. Nothing fails when it grows; the product just quietly stops
  knowing things.
* **`tokens_this_hour` against `tokens_cap`** tells you whether teams are
  hitting the ceiling. A cap teams reach every hour is a cap that is refusing
  work rather than preventing abuse.
* **`runs_by_status`** over the last 24 hours. A rising `failed` share is the
  first sign of a model or credential problem; a rising
  `waiting_for_permission` share means consent cards are piling up unanswered.

## Logs

Every service installs the same handler (`shared/observability.py`), which does
two things that a default `logging.basicConfig` does not.

**Correlation fields** are appended to every line as `key=value`:

```
2026-09-08T09:14:02 INFO agent.runtime: empty turn from the model (attempt 2/3) service=agent-worker team_id=… thread_id=… run_id=…
```

The fields are `service`, `team_id`, `thread_id`, `run_id`, `job_id`,
`job_type`, `worker_id`, `request_id`. They come from a context opened around
the unit of work, so a line logged deep inside a tool carries them without that
tool knowing it is inside a turn.

To investigate one member's complaint, get the run id from the thread and
filter on `run_id=`.

**Redaction is enforced at the handler**, not left to call sites. Every record
— message, arguments and traceback together — passes through
`shared/errors.py:redact`, which removes:

* Postgres' `DETAIL:`/`CONTEXT:` sections. On a constraint violation `DETAIL`
  is the *entire failing row*, which for this system is a member's message, a
  document excerpt, or a prompt.
* Connection-string passwords, `password=`, `Bearer` headers, and generic
  `key=`/`token=`/`secret=` forms.
* GitHub tokens (`ghp_`, `ghs_`, `gho_`, `ghu_`, `ghr_`, `github_pat_`), PEM
  blocks, and JWTs.

The same function guards every column that stores an error —
`jobs.last_error`, `agent_runs.last_error`, `documents.parse_error`,
`sandbox_cleanup.last_error` — because `jobs.last_error` is readable by the
cross-team control role and the others are shown to members.

If you add a new place an error is stored, redact at that boundary rather than
at its callers.

## Draining a worker

Both workers handle `SIGTERM` and `SIGINT` as *finish what is in hand, start
nothing more*:

* The pipeline worker completes the job it is running and claims no further
  job, including when the signal arrives while it is idle.
* The agent worker's slots each finish their current turn; `main()` joins them
  rather than leaving them daemonised, so the process does not exit mid-turn.

The first signal hands itself back to the default handler, so **a second
`SIGTERM` terminates immediately** — an operator who means it does not have to
reach for `SIGKILL`. (This is stated because it was not true until T27: a
persistent handler swallowed every signal after the first.)

```bash
docker compose stop pipeline-worker     # SIGTERM, then SIGKILL after the grace period
```

**The grace period must exceed the longest job you expect.** Work killed before
it finishes is not lost, but it is not instant either, and the two workers have
very different numbers:

| | grace period | lease | attempts | worst case before a retry |
|---|---|---|---|---|
| `pipeline-worker` | 120s | 30 minutes | 3 | **30 minutes** |
| `agent-worker` | 300s | 5 minutes | 3 | 5 minutes |

The pipeline row is the one to look at. A clone or a dependency build that is
still running at 120 seconds is killed, and the job then waits out a **thirty
minute** lease before any worker touches it again — so a deploy during a large
repository sync can leave that team's checkout stale for half an hour, with
nothing failing and nothing to see. Raise `stop_grace_period` above your p99
job duration, or accept the window knowingly.

That window is what a member experiences as "nothing is happening".

## Alerts worth having

Ordered by how much a member notices.

| Alert | Condition | First action |
|---|---|---|
| **Deployment not ready** | `/ready` non-200 for 2 consecutive checks | read `checks` — it names the failing one |
| **Turns not starting** | `checks.agent_queue` not ok | agent worker logs |
| **Pipeline stalled** | `checks.pipeline_queue` not ok | pipeline worker logs |
| **Rotation half-done** | `checks.roles` not ok | the named role's env var |
| **Schema behind** | `checks.migrations` not ok | run the migrate service |
| **Leases piling up** | `checks.expired_leases` not ok | is any worker alive |

`/ready` is the single thing to poll; every alert above is one of its checks.

## Budgets and spend

Two caps per team per hour, both in `usage_buckets` and both enforced inside
the statement that spends against them:

* `AGENT_TURNS_PER_HOUR` (60) — the turnstile.
* `AGENT_TOKENS_PER_HOUR` (500,000) — the one that tracks cost.

A turn reserves `AGENT_TOKENS_ESTIMATE` (6,000) at admission and reconciles the
truth when it finishes. It is also stopped *between model calls* if it passes
what the team has left, so one sweeping turn cannot spend the hour — the member
sees what it managed and an explanation.

To see spend:

```sql
select team_id, bucket, turns, tokens
  from public.usage_buckets
 where bucket > now() - interval '24 hours'
 order by tokens desc;
```

Cost in currency is deliberately null unless `GEMINI_INPUT_USD_PER_MTOK` and
`GEMINI_OUTPUT_USD_PER_MTOK` are set, because a price that differs per
deployment should not be guessed on someone else's behalf.

## Incident switches

**Stopping a worker is the safest general-purpose switch.** Work queues rather
than failing, and it resumes untouched when the worker comes back.

```bash
docker compose stop agent-worker      # no turns run; members' messages queue
docker compose stop pipeline-worker   # no ingestion, compilation or syncing
```

Feature switches, all of which fail closed when emptied:

| To disable | Set | Effect |
|---|---|---|
| previews | `COMRADE_PREVIEW_DOMAIN=` | previews refuse with a message saying they are not configured |
| dependency installs | `COMRADE_SETUP_PROXY_URL=` | setup is disabled rather than given open egress |
| the metrics endpoint | `COMRADE_METRICS_TOKEN=` | `/metrics` answers 404 |

### Two that look like switches and are not

**`AGENT_TURNS_PER_HOUR=0` does not stop turns — it makes them unlimited.**
Zero and below mean "no cap" in `shared/usage.py`, so setting it to disable the
agent removes the brake instead of applying it. To throttle rather than stop,
set a low positive number; to stop, stop the worker.

**Removing `GITHUB_WEBHOOK_SECRET` does not stop ingestion cleanly.** Every
delivery is then refused as unsigned, GitHub retries each one, and you get a
redelivery backlog to work through afterwards. Stop `pipeline-worker` instead:
deliveries are still accepted and queued, and drain when it returns.
