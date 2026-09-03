# Working on Comrade

Comrade is an AI teammate for engineering teams that run without a manager. It
reads a team's chat, documents and repository, compiles what the team has
decided into a cited wiki, and takes action only with a member's consent.

This file is for whoever — or whatever — is editing this repository. Comrade
reads exactly this file out of a *team's* repository (`agent/repo_tools.py`,
`repo_guide`) and puts it in front of its own model, so the shape is one it
already understands.

## Commands

```bash
uv sync                                  # backend deps
uv run pytest -q                         # backend suite
uv run uvicorn server.app:app --reload   # API on :8000
uv run python -m pipeline.worker         # job queue + chat capture

cd frontend && npm install
npm test -- --run          # unit + component
npm run test:integration   # against the live local stack
npm run test:e2e           # playwright journeys
npx tsc --noEmit           # typecheck
npm run lint               # oxlint
```

**Before merging, run the gates — not the commands above one at a time:**

```bash
scripts/gates.sh              # every lane
scripts/gates.sh --with-reset # also rebuilds the DB from migrations
```

## Five rules this codebase learned the hard way

### 1. Never discard a failure signal

Six separate defects on one branch shared this shape, in four languages:
`shutil.rmtree(ignore_errors=True)` that never deleted anything; an HTTP
cleanup that ignored 4xx; `pytest -q | tail && git commit`, which gates on
`tail`'s exit code and therefore on nothing; a retry announced through a
`console.warn` that printed nothing; and `/health` returning `ok` without
touching the database while every request failed on a dead pool.

If you write something that *can* fail and you are not raising, **say so out
loud** — a status code you did not check, an exception you swallowed, and a
warning nobody prints are the same bug.

### 2. A policy without a grant is dead code

Postgres checks table privilege **before** it consults RLS. So
`create policy ... for all to comrade_pipeline` on a table that role holds no
`UPDATE` on fails with a permission error, while the policy sitting next to it
reads as perfectly correct. This cost two debugging sessions before
`tests/test_wiring.py` started catching it.

Every migration that adds a policy adds the matching `grant` in the same file.

### 3. RLS is the API

The frontend talks to Supabase directly. A check that lives only in a FastAPI
route is bypassed by anyone posting to PostgREST. Authorisation belongs in a
policy; a route is for things a browser must not hold — a model key, the
executor role, a GitHub installation token.

There are four DB roles and no `service_role`: `comrade_agent` (reads,
proposes), `comrade_executor` (runs approved consent actions), `comrade_pipeline`
(parser, compiler, clones), `comrade_authenticator` (becomes `authenticated`).

### 4. The model never supplies identity

`team_id` and `requester_id` are bound server-side into ADK session state and
read from there by every tool. They are never LLM arguments. A tool argument the
model can name is a tool argument the model can be talked into naming wrongly.

Anything a person wrote reaches the model **datamarked** — spaces replaced with
`^` by `pipeline.parsers.spotlight`. That includes repository files and the
output of commands the agent runs. Marked text is data, never instruction.

### 5. Registration by import side effect needs a test

Job handlers register when their module is imported, and `pipeline/worker.py`'s
`main()` carries a hand-written import list. It was missing one, so every
`sync_repo` job failed with "no handler registered" — and every test passed,
because pytest imported the module itself.

`tests/test_worker_handlers.py` checks this in a **subprocess**. Any new
registry needs the same: two places that must agree fail somewhere other than
where they are caused.

## Local database

```bash
npx supabase start
npx supabase migration up                     # or: npx supabase db reset
uv run python scripts/restore_local_roles.py  # ALWAYS after a db reset
```

A reset drops `comrade_authenticator` and the passwords on the other three
roles — they are created by a script, not a migration. Skip that step and the
whole suite fails on authentication in a way that reads like broken migrations.

**Stop the worker before running the suite.** A live `pipeline.worker` drains
the queue the queue tests are draining, and the failures point nowhere near the
cause. `scripts/gates.sh` refuses to start while one is up.

## Running a team's code

`repo_run` executes the team's own code in a container (`agent/sandbox.py`):
no network, read-only rootfs, no capabilities, `.git` masked, memory/CPU/pid
capped. Comrade's process holds a database URL that owns every team's data, a
GitHub credential and a model key — none of them reach that container, and
`tests/test_repo_run.py` asserts it.

Build the image once:

```bash
docker build -f docker/sandbox.Dockerfile -t comrade-sandbox:latest .
```

The command allowlist is **legibility, not confinement**: `python` is arbitrary
code execution. The container is the boundary.

## Tests

Four layers, and the fourth is the one that matters most when git is involved:

| lane | what it proves |
|---|---|
| `pytest` | logic, RLS, consent, capability |
| vitest unit/component | view-models and rendering |
| vitest integration | supabase-js as real signed-in users |
| playwright | the journeys a member actually takes |
| `pytest -m realgithub` | clones and opens a real pull request |

`_url_for` and `_create_pr` are seams that let git be tested offline. They are
also where the expensive bugs hid — an empty repository, a CRLF-mangled patch,
a `--force-with-lease` that could not read its own ref. **Every seam that makes
testing easy needs one test that does not use it.**

Write the test that would have caught the bug, not the test that covers the
line. If a test passes both before and after your fix, it is not testing the
fix.
