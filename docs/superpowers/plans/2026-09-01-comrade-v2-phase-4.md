# Phase 4 — Memory depth

The last of the approved v2 scope. Seven features from the master plan's table,
re-sequenced on two dependencies the table does not encode.

Already shipped, so not repeated here: **§20.3.1** temporal annotation at render
(`pipeline/wiki.py` calls `annotate(f)`) and **§6.3-1** page descriptions
(`compiler.py:_resolve_page` fills a blank description, and the extractor is
prompted for `page_description`). Both were listed as prerequisites for the
recall design; both are done.

---

## The order, and why it differs from the table

| # | Feature | Why here |
|---|---------|----------|
| 1 | **F22** stage-1 extraction recall eval | Produces a number, not a feature. §20.3.2 calls extractor starvation "a larger practical risk than any retrieval-architecture question" and notes recall is **currently unmeasured**. Everything downstream is a change to extraction or a mitigation for its failures; measuring first is what makes any of them assessable. |
| 2 | **F26** consolidation context cap | §20.3.3. Cheapest item, one file, and it bounds the prompt every later feature adds facts to. F22's eval runs compiles, so the cap makes the eval cheaper too. |
| 3 | **F25** `memory_pages.scope` + `skill` kind | **Moved ahead of F23.** See below. |
| 4 | **F23** `memory_search(query)` | Written scope-aware from the start rather than retrofitted. |
| 5 | **F24** explicit "remember this" | §20.7.1's mitigation for starvation — now measurable against F22's baseline. Routes through the compiler, so it wants F25/F26 settled first. |
| 6 | **F27** anchored comments on entries | Self-contained: one table, two components. |
| 7 | **F37** publish artifact from private thread | Fully independent of the rest — migration plus two screens, no backend. |

**The one real dependency: F25 before F23.** `memory_search` ranks over
`memory_versions.fact`. The moment `memory_pages.scope` exists, a search that
does not filter on it surfaces restricted facts to anyone who asks — the same
shape as the four RLS holes found this week, and the same reason
`contribution_v` leaked. Building search after scope means it is scope-aware by
construction instead of by a follow-up fix. Cheap to reorder, expensive to
forget.

---

## Decision I am taking, stated rather than assumed

**`scope` ships as `team` and `restricted` only. Not `personal`.**

§4.5 lists three values, and §20.7.3 records the tension honestly: a personal
store cuts against §14's flat, visible, shared-context thesis, and §6.2-1 parked
the L3 persona layer deliberately. Shipping `personal` inside Phase 4 would
un-park that decision as a side effect of adding a column.

`team` and `restricted` are enough for what §24.2 actually asks for — a `skill`
page is a shared standard, which is the *most* team-scoped thing in the system.
Adding `personal` later is one line in a CHECK constraint.

Flagging rather than burying: if the intent was always three values, say so and
it goes in with the same migration.

---

## Per-feature notes

### F22 — stage-1 extraction recall (`evaluation/`)

The existing harness (`evaluation/runner.py`, `scoring.py`) scores an agent's
**tool sequence** against a scenario. Extraction recall is a different question
and needs its own scorer: given a held-out document with hand-labelled expected
facts, what fraction did `extract_candidates` produce?

Deliberately not a framework (§20.3.2: *"a held-out set of documents with
hand-labelled expected facts is sufficient"*). Matching is the only real design
question — exact string match will under-report badly, so match on a normalised
containment check and record the raw pairs so a human can audit disagreements.

Marked `-m live` like the other model-calling tests; a recall number from a
mocked extractor measures nothing.

### F26 — consolidation cap (`pipeline/compiler.py`)

`build_consolidation_prompt` sends the whole wiki. Cap the fact count, and be
explicit about what is dropped: silently truncating memory is the failure mode
the cap exists to prevent from happening by accident. Prefer recency and the
pages the candidates actually touch; log what was excluded.

### F25 — `scope` + `skill` (migration, `compiler.py`, `wiki.py`, `Wiki.tsx`)

Two columns on `memory_pages`: `scope` and `kind` (`fact` | `skill`). A skill
page is procedural — §24.2's "playbook everyone knows cold" — so it renders as
prose rather than a bullet list, and the extractor needs to be able to propose
one.

RLS: `restricted` pages need a policy, and every existing read path
(`all_active_pages`, `memory_read_page`, the wiki screen) needs to respect it.
That is the risky part of this feature, not the column.

### F23 — `memory_search(query)` (migration, `agent/tools.py`)

`tsvector` on `memory_versions.fact` + `ts_rank_cd`, capped at N, returning
facts with citations and dates. Native Postgres, inside RLS, inside the existing
transaction. **No pgvector** — §20.4-3 makes that a separate later decision taken
only if lexical measurably misses.

Registry entry: `db`, read-only, `needs_human=False`, like the other reads.

### F24 — "remember this" (`server/app.py`, `pipeline/chat.py`, `GroupRoom.tsx`)

Members cannot write `memory_*` — that is `comrade_pipeline` alone. So this
enqueues a compile job carrying the member's text as a single spotlighted
candidate: extract → consolidate → apply, earning a citation, a diff card and a
revert like anything else. §20.7.1 is explicit that routing through the compiler
is what keeps the datamarking guarantee; a direct write would open an
unspotlighted path into agent context.

### F27 — anchored comments (migration, `Wiki.tsx`, `MemoryDiffCard.tsx`)

A comment attaches to a `memory_entries` row and survives revision — the anchor
is the entry, not the version, or every compile orphans the discussion. §6.3-6's
"disagreement collapses to revise, the losing side vanishes" is the gap; a
comment thread is where the losing side stays visible.

### F37 — publish from private thread (migration, `types.ts`, two screens)

`messages.ai_assisted boolean not null default false`, with
`check (not ai_assisted or sender_kind = 'user')` — an AI message claiming to be
AI-assisted is nonsense. No endpoint and no consent path: `au_messages_insert`
already requires `sender_kind='user' and sender_id = auth.uid()`, and RLS gates
rows, not columns.

Q8 settled this in the v2 plan: **current team only**, no cross-team picker.
The prefilled body must be editable before it carries the member's name.

---

## Gates

Same as every prior slice: full backend suite, frontend unit + integration +
e2e, a from-scratch `supabase db reset` with `scripts/setup_local_roles.sql`
re-applied, and a mutation check on anything load-bearing. Nothing merges on a
suite I have not watched go red for the right reason first.
