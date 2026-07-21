# Give the agent its memory, and stream the turn — Design

Date: 2026-07-21 · Status: approved (owner, in-session)
Applies Claude Code's context model to Comrade where it genuinely fits.

## The problem: the agent is memory-blind

`grep -rn "memory\|wiki" agent/*.py` returns nothing. The agent has four tools
— `team_get_state`, `team_propose_task`, `team_propose_group_message`,
`member_send_nudge` — and `team_get_state` returns team, members, tasks and
pending consent. Nowhere does the agent read `memory_pages`,
`memory_entries`, `memory_versions` or `memory_citations`.

So the whole memory arc — two-stage compiler, wiki pages, bi-temporal
versions, citations, chat capture, reverts — produces a wiki that **the AI
never reads**. Ask Comrade "what's our deadline?" and it answers from the
`tasks` table, not from the team's compiled knowledge. From the agent's side
the memory system is write-only.

`pipeline/wiki.py::page_index()` already exists, is called by nothing, and its
own docstring calls it "the future agent-recall index". It was built for this
and never wired up.

## What we copy from Claude Code, and what we don't

| Claude Code | Comrade today | This slice |
|---|---|---|
| `CLAUDE.md` auto-loaded every session | agent sees nothing | auto-load the wiki **page index** into the instruction |
| Skills: description always in context, body loaded on demand | whole wiki or nothing | `memory_read_page(title)` tool |
| Cites `file:line` | facts already carry citations | the tool returns citations; the agent cites them |
| Streams tokens, shows tool calls live | steps logged to `agent_runs`, never surfaced | stream the turn |
| Hooks: deterministic code at lifecycle points | DB triggers + guards | already converging — nothing to do |
| Permission modes / "always allow" | consent tiers T0–T3 | already ahead — nothing to do |
| Compaction | — | not needed at this scale |

**This is not the RAG we deleted.** No embeddings, no vector store, no
similarity search. The model chooses a page by reading titles and
descriptions — the same way Claude Code chooses a skill. What was deleted was
vector retrieval; this is the index pattern that replaces it.

## A. Auto-loaded page index

ADK accepts `instruction: str | Callable[[ReadonlyContext], str | Awaitable[str]]`,
so the instruction is built per turn. `team_id` is already server-bound into
session state by `agent/runtime.py`.

The builder appends to the static instruction:

```
## The team wiki
Compiled from the team's documents and chat. Facts here are cited, versioned
and revertible by any member.

- Deadlines — key dates and their sources
- Scope — what is in and out of v1
(…titles + descriptions only…)

Call memory_read_page with a title to read a page's facts before answering
questions about decisions, deadlines, scope or history.
```

Empty wiki → a single line saying so, and the agent is told to say the wiki is
empty rather than guess.

**Why the index and not the whole wiki:** the compiler already sends the whole
wiki on every compile, and that cost is unbounded. Auto-loading the whole wiki
into every *turn* would repeat that mistake on a hotter path. The index is
bounded by page count; page bodies load only when the model asks. This also
means nothing is ever silently dropped — the earlier idea of capping and
discarding pages would have degraded answers invisibly.

## B. `memory_read_page` tool

```python
def memory_read_page(tool_context: ToolContext, title: str) -> dict
```

`team_id` comes from session state (server-bound, never a model argument —
same rule as every other tool). Returns:

```python
{"title": str, "description": str,
 "facts": [{"fact": str,
            "citations": [{"source_kind": "message"|"document"|"github",
                           "source_id": str, "excerpt": str | None}]}]}
```

Unknown title returns `{"error": "no such page", "available": [titles]}` so the
model can correct itself in one step rather than hallucinate.

Reads run under `Role.AGENT`, which already holds `select` on every `memory_*`
table and is scoped by `team_session`, so RLS confines it to the current team.
No new grants.

## C. Streaming the turn

Today `POST /agent/turn` blocks for the whole Gemini call, then returns
everything at once. The steps are already recorded to `agent_runs` and never
shown to anyone.

**Transport: newline-delimited JSON over `fetch`, not SSE.** `EventSource`
cannot send an `Authorization` header, and putting a JWT in the query string
would leak it into logs and history. `fetch` + `ReadableStream` carries the
header normally, and NDJSON needs no frame parsing — one `JSON.parse` per line.

New endpoint `POST /agent/turn/stream`, emitting:

```
{"type":"run","run_id":"…"}
{"type":"step","tool":"team_get_state"}
{"type":"text","text":"The demo is "}
{"type":"done","reply_message_id":"…","user_message_id":"…"}
{"type":"error","detail":"…"}
```

`POST /agent/turn` stays exactly as it is — scripts, smoke tests and the whole
existing test suite keep working. The two endpoints share the persistence
helpers that already exist (`_persist_user_message`, `_persist_ai_reply`).

**Membership and the turn budget are checked before the stream opens**, so a
non-member still gets a real `403` and an over-budget team a real `429` —
never a `200` whose first frame is an error.

`agent/runtime.py` grows `stream_turn()`, an async generator that yields step
dicts as they arrive and records them as it goes. `run_turn()` becomes a
consumer of it, so there is one implementation and two shapes rather than two
copies of the orchestration.

Sync DB writes inside the generator (`start_run`, `append_step`, `finish_run`)
are wrapped in `run_in_threadpool` so they never stall the event loop.

**Frontend scope is deliberately small:** the existing typing indicator
accumulates streamed text, and tool steps render as one muted status line
("checking the team's state…"). The message renderer is untouched; the final
message still arrives via Realtime exactly as now.

## Testing

1. Instruction builder includes page titles for a team with a wiki, and the
   empty-wiki line for one without.
2. `memory_read_page` returns facts with citations; an unknown title returns
   the available list; a page from another team is invisible.
3. The stream endpoint emits `run` → `step`/`text` → `done` and persists both
   messages.
4. `403` and `429` are returned as HTTP statuses before any streaming begins.
5. Existing `/agent/turn` tests keep passing unchanged.
6. Live: one real Gemini turn where the agent answers from a seeded wiki fact
   and cites it (`-m live`).

## Explicitly skipped

| Skipped | Add when |
|---|---|
| Interrupting a running turn | members report runaway turns |
| Streaming `/agent/turn` itself (replacing the batch endpoint) | nothing non-browser calls it any more |
| Wiki-index caching between turns | the index query shows up in latency |
| Agent *writing* memory | never — the compiler is the sole writer, by design |
