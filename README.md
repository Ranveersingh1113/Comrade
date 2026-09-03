# Comrade

An AI teammate for engineering teams that run without a manager. It sits in a shared
team room as a silent member — reading chat, documents, deadlines, and repository
activity — and helps the team coordinate by compiling what the team has decided into
a cited wiki, surfacing accountability gaps, and taking action with a member's consent.

The pitch is not "saves you time". Execution already got cheaper; planning did not.
At the volume agentic tooling now produces, nobody holds the shared picture any more.
Comrade's territory is **coherence at speed**.

The primary target is developer and engineering teams — connecting a repository is a
core feature, not an integration. Any small self-organising team fits: startups,
enterprise sub-teams, student project groups. Student teams are a pilot beachhead
served by the free tier, not the ceiling.

Two invariants shape the whole codebase:

- **The agent never performs a group-visible action it chose itself.** It proposes; a
  human approves; a separate database role executes. See `shared/consent.py`. Two
  group-visible AI writes are not exceptions to this, because neither is the agent
  acting on its own initiative: its reply when a member asks it something in the room,
  and the memory compiler's diff card, which is a system notice.
- **The model never supplies identity.** `team_id` and `requester_id` are bound
  server-side into ADK session state and read from there by every tool — never
  passed as LLM arguments.

## Stack

- **Agent:** Google ADK (Python), single `LlmAgent` on Gemini 2.5 Flash (Pro for escalation)
- **Backend:** Supabase (Postgres + Realtime + Auth + Storage)
- **Frontend:** React + TypeScript + Vite SPA; talks to Postgres directly under RLS,
  and to the FastAPI service only where a key or role must stay server-side
- **Tools:** ADK native function tools (19) — team state, wiki, chat and document
  search under team-scoped worker roles; and the repository tools, which read,
  edit and run a team's connected checkout and propose changes as pull requests
- **Memory:** Gemini two-stage compiler with cited, versioned wiki facts; vector
  retrieval is intentionally not part of the current design
- **Eval:** deterministic tool-routing checks, with optional live-model smoke tests

Access control is enforced by Postgres row-level security, not by application code.
Every worker connects under one of four RLS-bound roles — `agent`, `executor`,
`pipeline`, `admin` — and never as `service_role`.

## Repository layout

| Path | Purpose |
|------|---------|
| `agent/` | Google ADK `LlmAgent`, its function tools, the capability layer, the container sandbox, and the turn runtime |
| `server/` | FastAPI service — agent turns, consent resolution, invites, document ingest |
| `pipeline/` | Job worker, document parsers, and the two-stage memory compiler |
| `shared/` | Config, RLS-bound DB sessions, consent mechanism, nudges, run logging |
| `frontend/` | React + Vite SPA (screens, hooks, pure view-models, 4-layer test suite) |
| `supabase/` | Supabase config + SQL migrations (`supabase/migrations/`) |
| `evaluation/` | Tool-routing eval against a live model |
| `scripts/` | Local role setup SQL + smoke scripts |
| `tests/` | pytest unit, integration, and live-model smoke suites |
| `docs/` | Architecture and research findings |

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12 (uv manages this automatically).

```bash
uv sync                 # create the virtualenv and install dependencies
cp .env.example .env    # then fill in real values (never commit .env)
```

The frontend has its own dependencies and environment:

```bash
cd frontend && npm install
```

It needs `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`, and optionally
`VITE_AGENT_API_URL` (defaults to `http://localhost:8000`). `lib/supabase.ts`
throws at import if the first two are missing, so a misconfigured environment
fails immediately rather than at the first query.

For the database-backed test suite and workers, also start the local Supabase
stack, apply migrations, and create the local worker login roles as described
in [HANDOFF.md](HANDOFF.md#8-running-the-stack-locally).

Connecting a repository needs a GitHub App — the credential is minted per
installation, scoped by GitHub to the repositories that installation was
granted, and never stored. `.env.example` lists the settings that are easy to get
wrong; the one worth repeating is the **Callback URL**, which must be exactly
`<frontend>/github/setup`. Not the Setup URL — ticking "Request user
authorization (OAuth) during installation" disables that field, and GitHub
redirects to the Callback URL instead. The path carries no team because an App
has only one such URL; the signed state token carries it.

**Comrade follows your repository's own conventions.** If the repo has an
`AGENTS.md`, `CLAUDE.md` or `.cursorrules`, it is read at the start of every
turn and put in front of the model — the same file your other coding tools
already use, so there is nothing extra to write. It arrives datamarked and
framed as data: it *informs* Comrade and cannot override the consent rules,
which matters because a repository that accepts pull requests accepts them
from strangers.

Without an App, Comrade still ingests repository history from webhook
deliveries — that path holds no credential. Only the working copy needs one.

The agent runs a team's own code (`repo_run`) inside a container and never on
the host, so Docker must be running and the sandbox image must exist:

```bash
docker build -f docker/sandbox.Dockerfile -t comrade-sandbox:latest .
```

Without it `repo_run` refuses with the build command rather than falling back
to the host — running an arbitrary repository's test suite uncontained would
hand it the database URL, the GitHub credential and the model key that
Comrade's own process holds.

## Run it

Three processes, each in its own terminal:

```bash
uv run uvicorn server.app:app --reload      # API on http://127.0.0.1:8000
uv run python -m pipeline.worker            # job queue + ambient chat capture
cd frontend && npm run dev                  # SPA on http://localhost:5173
```

The worker drains the job queue, then sweeps every team's group chat into memory
once at least five new messages have accumulated past the last watermark. Without
it, uploaded documents stay in `parsing` forever.

### Calling the API directly

Identity comes from a Supabase-issued JWT and nothing else — there is no way to
act as another user by naming them in the body. `team_id` is still a body field,
because a user belongs to many teams and it is a routing choice, but every
endpoint re-checks membership through the caller's own RLS context.

```bash
curl -s http://127.0.0.1:8000/agent/turn \
  -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"team_id":"<team-uuid>","text":"Give me a status summary.","thread_type":"group"}'
```

`POST /agent/turn/stream` runs the same turn as newline-delimited JSON, one object
per line — NDJSON over `fetch` rather than SSE, because `EventSource` cannot send an
Authorization header. Membership and per-team rate-limit checks both run before the
response starts, so a non-member gets a real 403 and an over-budget team a real 429.

Every turn is recorded to `public.agent_runs` — one row per turn, one step per tool
call, tool result, and text chunk — for observability and crash recovery.

## Before merging

```bash
scripts/gates.sh              # everything except the destructive migration check
scripts/gates.sh --quick      # skip the browser and real-GitHub lanes
scripts/gates.sh --with-reset # also rebuild the database from migrations
```

`--with-reset` rebuilds the database from every migration and re-runs the
suite. It also restores the worker LOGIN roles afterwards
(`scripts/restore_local_roles.py`), because `supabase db reset` drops
`comrade_authenticator` and the passwords on the other three — they are created
by a script, not a migration, and without that step the whole suite fails on
authentication in a way that reads like broken migrations.

It is a script rather than a list of commands because `pytest -q | tail && …`
gates on nothing: the pipe makes the exit status `tail`'s, which is always 0.
It also refuses to run while a `pipeline.worker` is up, since a live worker
drains the queue the queue tests are draining and the resulting failures point
nowhere near their cause.

Two lanes are excluded from the default `pytest` run and included here:

| marker | what it does |
|---|---|
| `live` | talks to Gemini — paid and nondeterministic |
| `realgithub` | clones and opens a real pull request, then closes it and deletes the branch |

`realgithub` is the one that does not fake its dependencies. Everything else
drives git against a local bare repository through the `_url_for` and
`_create_pr` seams — which is exactly where the empty-repo, CRLF and
force-with-lease bugs hid. It skips itself when no credential is configured,
and `COMRADE_E2E_REPO` picks the scratch repository.

## Architecture

[docs/architecture.md](docs/architecture.md) traces every flow function by function:
the agent turn, the consent lifecycle, document ingest, chat capture, and the
frontend tree.
