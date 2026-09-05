# Running Comrade

Written 2026-09-05, for the first deployment that other people use.

Read the **Ceilings** section before you let anyone in. It is not a list of
nice-to-haves; it is the set of things that are deliberately not done yet, and
two of them decide who you can safely invite.

---

## What runs where

Four processes and a database. The split is not arbitrary — it comes from one
constraint:

> `repo_run` executes a team's code in a Docker container. The agent worker
> therefore needs a Docker daemon. A normal application container on
> Fly/Render/Railway does not have one.

| process | needs | can run on |
|---|---|---|
| `api` (`uvicorn server.app:app`) | Postgres | anywhere |
| `pipeline-worker` (`python -m pipeline.worker`) | Postgres, git, disk | anywhere with a volume |
| `agent-worker` (`python -m agent.worker`) | Postgres, git, disk, **Docker socket** | a VM, or a Fly machine with Docker |
| `frontend` (nginx) | nothing at runtime | anywhere, or a static host |
| Postgres | — | **Supabase**, not a bare postgres container |

Supabase specifically, and this is load-bearing: Comrade's authorization model
is RLS plus four separate database roles plus `auth.uid()`. A plain Postgres
image has none of that, and running without it does not degrade the security
model — it removes it.

### The sandbox is a sibling, not a child

The agent worker mounts `/var/run/docker.sock` and asks the **host** daemon to
start sandbox containers. Those containers bind-mount the thread's worktree, and
the path in that bind mount is resolved by the host, not by the worker.

So the workspaces volume must be visible at **the same path** inside the worker
and on the host. In the compose file that is `/workspaces` for both. If you
change it, change `COMRADE_WORKSPACES_ROOT` and the host mount together, or
every `repo_run` will fail with a mount error naming a directory that exists in
one namespace and not the other.

---

## First deploy

Steps a person has to do are marked **you**. Those involve creating accounts,
entering credentials, and authorising OAuth, which is not something to automate
away.

1. **you** — Create a Supabase project. Note the project URL, the anon key, the
   service secret and the JWT secret.
2. Apply the schema:
   ```bash
   npx supabase link --project-ref <ref>
   npx supabase db push
   ```
3. **you** — Create the four worker roles and their passwords. `npx supabase db
   push` does not create them; they are login roles, not schema:
   ```bash
   uv run python scripts/restore_local_roles.py
   ```
   Run it against the deployed database, and use real passwords rather than the
   local development ones.
4. **you** — Get a Gemini API key. This is the key every team's turns are billed
   to, which is why the quota work exists — see **Money** below.
5. **you** — Create a GitHub App (`GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`,
   `GITHUB_APP_SLUG`, `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET`,
   `GITHUB_WEBHOOK_SECRET`). Point its webhook at `https://<api>/webhooks/github`.
   Do **not** set `GITHUB_PAT` in a deployment: it is scoped to everything its
   owner can reach, and `_pat_is_still_single_tenant()` refuses it the moment a
   second team connects a repository.
6. Copy `.env.example` to `.env` and fill it in. The database URLs point at
   Supabase's pooler, not at `127.0.0.1:54322`.
7. Build the sandbox image **on the host that runs the agent worker**:
   ```bash
   docker build -f docker/sandbox.Dockerfile -t comrade-sandbox:latest .
   ```
8. `docker compose up -d --build`
9. Check `https://<api>/ready` and expect `{"status": "ready"}`. If it says
   `not_ready`, the `checks` object names which subsystem.

## Deploy order, every time after that

**Expand, migrate, then contract.** The schema is deployed before the code that
needs it, and columns are dropped only after nothing reads them.

1. `npx supabase db push` — new migrations.
2. Deploy the app image.
3. `GET /ready` — `migrations` reports `behind` if step 1 was skipped, which is
   the failure this check exists for: an app deployed ahead of its schema fails
   on the first request touching a new column, and reads as a code bug.

Rolling back the CODE is safe. Rolling back a **contract** migration is not —
`20260904100000_threads_contract.sql` drops columns, and the data in them is
gone. Restore from a backup rather than reversing it.

## Stopping

Both workers catch `SIGTERM` and finish the item in hand before exiting. Give
them time to: `stop_grace_period` is 120s for the pipeline worker and 300s for
the agent worker, because an agent turn can be a model call plus a container
run. Killing one early is not data loss — the lease expires and the run is
reclaimed — but it is minutes of a member watching nothing happen.

## Money

Every team's turns bill to your `GEMINI_API_KEY`. Two settings bound it, per
team per hour:

- `AGENT_TURNS_PER_HOUR` (default 60)
- `AGENT_TOKENS_PER_HOUR` (default 500,000)

Both are enforced atomically against `usage_buckets` — a turn reserves
`AGENT_TOKENS_ESTIMATE` (6,000) in the same statement that checks the cap and
reconciles the real cost when it finishes. Setting either to `0` removes that
limit, which on a public deployment means an unbounded bill.

`agent_runs.cost_usd` is populated from `GEMINI_INPUT_USD_PER_MTOK` and
`GEMINI_OUTPUT_USD_PER_MTOK`. Set them, or your cost reporting is zeroes.

## Backup and restore

Supabase takes the backups. What matters is having **restored** one before you
need to:

```bash
npx supabase db dump --db-url "$COMRADE_DB_URL_ADMIN" -f backup.sql
# into a disposable local project, never the live one
npx supabase db reset && psql "$LOCAL_ADMIN_URL" -f backup.sql
uv run python scripts/restore_local_roles.py
uv run pytest -q
```

The suite passing against restored data is the check. Roles are **not** in the
dump — they are created by a script — so a restore without step 3 fails every
login and reads as though the schema is broken.

Workspaces are not backed up and do not need to be. A thread's worktree is
derived from a GitHub repository, and anything worth keeping has been proposed
as a pull request. Losing the volume costs a re-clone.

---

## Ceilings

Things deliberately not done. Each says who it affects and what closes it.

### 🔴 Production workers run as the table owner

`Role.ADMIN` — the RLS-bypassing owner — is still used at sixteen call sites
across `pipeline/`, `server/` and `shared/`. RLS remains the boundary for the
**browser**, which is the multi-tenant surface a user touches, so this is not a
hole a member can reach. It means a bug in our own worker code is not contained
by the database.

Task 23 of the multiplayer-harness plan replaces these with a narrow
`Role.CONTROL`. It was not attempted under a deadline because the failure mode
of getting one grant wrong is a query that silently returns zero rows, which is
this codebase's most common defect and its least visible.

### 🔴 Dependency installation reaches the whole internet

`run_setup` is the one phase with the network on, and it needs it — installing
means fetching. It runs as root in the container, on a writable volume, and
`pip install` runs `setup.py` while `npm install` runs postinstall scripts. It
holds **no** Comrade credentials, which is what makes it survivable at all, and
it is **opt-in per repository** (`env_enabled`), so no team gets it by
connecting a repository.

`PLANNED_SETUP_EGRESS_ALLOWLIST` in `agent/sandbox.py` records the policy that
is not yet enforced. Until a registry proxy exists, treat "this team enabled
their environment" as "this team may execute their dependency graph with
egress" — because that is what it is.

### The frontend integration lane fails open

Without `VITE_SUPABASE_URL` the integration tests skip 19 RLS isolation cases
and exit 0, and `gates.sh` counts that as a pass. Set the variable in CI, and
do not trust a green integration lane that did not print 19.

### Sandbox previews and CI feedback do not exist

Tasks 17 and 18 are not built. A team can run finite commands; they cannot see
a development server, and a failed GitHub check does not come back to the
thread that opened the pull request.

### `npm ci` does not work

`package-lock.json` is out of sync with `package.json` (`Missing:
@emnapi/core`). The frontend image uses `npm install` instead. Fixing the lock
is a separate change.

---

## When something is wrong

| symptom | look at |
|---|---|
| `/ready` says `migrations: behind` | `npx supabase db push` was skipped |
| `/ready` says `agent_queue: N run(s) queued` | the agent worker is down or cannot reach Postgres |
| every login fails after a restore | worker roles were not recreated (`restore_local_roles.py`) |
| `repo_run` fails with a mount error | workspaces path differs between host and worker |
| turns refused with 429 | the team's hourly cap; `usage_buckets` shows the spend |
| a member sees no reply and no error | check `agent_runs.last_error` — an empty model turn records its reason there |
