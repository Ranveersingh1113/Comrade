# Comrade

An AI companion for student group projects. It sits in a shared team room as a silent
member — reading chat, documents, deadlines, and GitHub activity — and helps the team
coordinate by monitoring communication, surfacing accountability gaps, engaging with
documents, and taking action (with consent for anything visible to others).

This is a solo-dev pilot targeting small student teams (≤4 members).

## Stack

- **Agent:** Google ADK (Python), single `LlmAgent` on Gemini 2.5 Flash (Pro for escalation)
- **Backend:** Supabase (Postgres + pgvector + Realtime + Auth + Storage)
- **Tools:** MCP — GitHub MCP server + a custom platform MCP server (FastMCP, stateless)
- **Embeddings:** OpenAI `text-embedding-3-small` + pgvector
- **Eval:** DeepEval (deterministic metrics only)

## Repository layout

| Path | Purpose |
|------|---------|
| `agent/` | Google ADK `LlmAgent` and its wiring |
| `mcp_server/` | Custom platform MCP server (FastMCP) |
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
