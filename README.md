# Comrade

An AI companion for student group projects. It sits in a shared team room as a silent
member — reading chat, documents, deadlines, and GitHub activity — and helps the team
coordinate by monitoring communication, surfacing accountability gaps, engaging with
documents, and taking action (with consent for anything visible to others).

This is a solo-dev pilot targeting small student teams (≤4 members).

## Stack

- **Agent:** Google ADK (Python), single `LlmAgent` on Gemini 2.5 Flash (Pro for escalation)
- **Backend:** Supabase (Postgres + pgvector + Realtime + Auth + Storage)
- **Tools:** ADK native function tools (call the DB under team-scoped worker roles); GitHub integration TBD
- **Embeddings:** OpenAI `text-embedding-3-small` + pgvector
- **Eval:** DeepEval (deterministic metrics only)

## Repository layout

| Path | Purpose |
|------|---------|
| `agent/` | Google ADK `LlmAgent` and its function tools |
| `server/` | FastAPI runtime — `POST /agent/turn` runs one agent turn |
| `pipeline/` | Document pipeline worker (PDF/.docx/WhatsApp ingestion) |
| `supabase/` | Supabase config + SQL migrations (`supabase/migrations/`) |
| `shared/` | Shared config, models, and utilities |
| `tests/` | pytest + DeepEval suites |
| `comrade-canvas (3)/` | Planning & research docs (design source of truth) |

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12 (uv manages this automatically).

```bash
uv sync                 # create the virtualenv and install dependencies
cp .env.example .env    # then fill in real values (never commit .env)
```

## Run the agent service

```bash
uv run uvicorn server.app:app --reload      # serves on http://127.0.0.1:8000

curl -s http://127.0.0.1:8000/agent/turn \
  -H 'content-type: application/json' \
  -d '{"team_id":"<team-uuid>","requester_id":"<user-uuid>","text":"Give me a status summary."}'
```

Every turn is recorded to `public.agent_runs` (one row per turn, one step per tool
call / result / text chunk) for observability. `team_id` / `requester_id` are
bound server-side; sourcing them from the Supabase JWT is the next task.
