# Comrade — Memory & Document-Ingestion Findings

> Date: 2026-06-28. Produced from a 6-lens web-backed research pass + 2 synthesis passes, weighed against
> independent judgment. Decisions below were reviewed and accepted by the owner.
> Status: **findings + accepted direction — no code changes yet.**

---

## Headline decisions

1. **RAG verdict = HYBRID.** RAG is demoted from "the architecture" to **one index among four**. Keep
   pgvector as the *candidate finder*, not the answer.
2. **The owner's two ideas are the same bet.** "llm-wiki-2" = Andrej Karpathy's **LLM Wiki** pattern; its
   community "v2" adds a typed knowledge graph over the pages = the owner's idea #2. **Comrade's
   compiled-facts memory already IS an LLM Wiki** (built in Postgres rows instead of markdown).
3. **The highest-ROI fix is neither idea:** replace the compiler's "feed ALL active facts in one Gemini
   call" with **top-k retrieval of only the relevant existing facts**. Do this first.
4. **Keep memory in Postgres; reject markdown-as-storage** (owner agreed, overruling the initial intuition).
5. **Sequence the graph AFTER the top-k fix + ingestion router** (owner agreed).

---

## The owner's intuition, verified

- **"Upload ≠ RAG"** — TRUE for a single small doc and local coding agents (Claude Code uses agentic
  search, not RAG); FALSE at scale — claude.ai **Projects auto-switches to RAG once the knowledge base
  exceeds the context window**, ChatGPT chunks+embeds every file. Comrade is the knowledge-base case →
  keep retrieval.
- **"Memory is a txt/md file"** — TRUE only for Claude Code (`CLAUDE.md`). Consumer Claude/ChatGPT memory
  is a structured store with retrieval. Comrade's compiled bi-temporal cited facts are a *more*
  production-grade analog. Markdown loses RLS, bi-temporal SQL, vector index, concurrent writes.
- One-liner: **emulate Claude's *ingestion* (multimodal live-read of fresh files); keep your own *memory*
  (compiled, cited, bi-temporal in Postgres). Don't converge them.**

---

## RAG demoted: retrieval by query class

| Query class | Mechanism | Comrade implementation |
|---|---|---|
| "Find the fact/doc/thread about X" (fuzzy, single-hop) | **Vector search** | pgvector HNSW on active `memory_versions` (KEEP) |
| "Who owns/blocks/decided what, where" (relational, multi-hop) | **Graph edges** | new `memory_edges` + recursive CTE (LATER) |
| "What *exactly* does the spec/sheet say" (precise, auditable) | **Agentic read of original** | retrieve via `memory_citations` → live-read bytes into Gemini multimodal |
| "Just-uploaded doc, answer now" (ephemeral) | **Live-read into context** | parse → stream full text/image/PDF into Gemini, write nothing |
| Whole team corpus < ~200K tokens | **Long-context + prompt caching, skip retrieval** | fast path for young teams (Anthropic) |

**Classic chunk-RAG is needed in exactly one narrow case:** a single uploaded doc too large for the
live-read budget that a member wants to Q&A *now* → ephemeral per-doc scratch index. **Do NOT build a
global cross-document chunk index** (semantic-collapse / swamping failure mode; the fact layer does this
better).

---

## The robustness answer: a per-file-type ingestion router

Principle: **prose → embed; structured data → structured extraction/query; layout/visual → vision model.**
A single `extract→chunk→embed` pipeline is provably wrong for everything but prose.

**Stage 0 — Sniff** with Magika (never trust client MIME/extension). Guards here: per-file size cap;
zip-bomb defense (decompression-ratio + entry-count cap — `.docx/.pptx/.xlsx` are zips); async parse
timeout (the `FOR UPDATE SKIP LOCKED` worker already gives retry/dead-letter; `jobs.status='failed'`
exists).

**Stage 1 — Route + three-way disposition (facts / retrievable original / summary):**

| Type | Handling | Note |
|---|---|---|
| Prose PDF | PyMuPDF4LLM → **escalate to Gemini multimodal if text density low** | PyMuPDF returns **empty on scanned PDFs** = silent loss today |
| Excel/CSV | **Never chunk-embed rows.** Extract scalar facts; keep sheet as queryable artifact (text-to-SQL) | semantic collapse + embedding swamping |
| Code (uploaded) | tree-sitter / cAST AST-chunking | line chunking splits functions |
| Code (repo) | **Agentic live-read** (existing GitHub path), not pre-embed | mirrors Claude Code |
| Slides (pptx) | per-slide; **vision-caption diagrams**; keep notes | layout, not prose |
| Image/diagram | **Direct to Gemini multimodal** (caption = embeddable text, image = cited source) | the "Claude streams base64" pattern |
| Word/Markdown | python-docx / markitdown (current happy path — KEEP) | |
| Link/article | fetch + readability → Markdown; URL as citation | |
| Transcript | segment by speaker/turn; extract decisions/action-items (shares code path with chat→memory) | |
| **Unknown** | **markitdown → multimodal → store-raw+1-line-summary → quarantine (`status='unsupported'`)** | **degrade to "stored, not extracted," never "lost"** |

---

## Memory model evolution (keep the spine; four moves)

Keep: `memory_entries → memory_versions` (bi-temporal, `is_active`) → `memory_citations`,
`memory_reverts`, compiler-only-writes, spotlighting/datamarking, memory-as-hint.

1. **INVALIDATE + dedup + entity-resolution** (biggest gap; ADD/REVISE-only today). Never hard-delete —
   set `is_active=false, valid_until=now()`. Tiered entity resolution: exact `ref_id` → fuzzy label →
   **embedding cosine over active facts (reuse the 1536-dim vectors)** → LLM tiebreaker. This is also the
   **memory-poisoning retraction** path (you currently can't retract a bad fact).
2. **Entity+edge graph, Postgres-native (LATER):**
   - `memory_entities(id, team_id, kind ∈ member|task|doc|decision|thread, label, ref_id→live row, embedding vector(1536))`
   - `memory_edges(team_id, src_entity, dst_entity, type ∈ owns|blocks|relates_to|discussed_in|decided_in, valid_from, valid_until, is_active, cited_in)`
   - Edges emitted by the **same** Gemini structured-output call that extracts facts (near-zero added
     cost). Closed 5-node/5-edge ontology — do NOT allow open-ended schema. Recursive CTE for 1–2 hops.
   - **No graph DB, no Apache AGE** (Supabase doesn't ship it), no Microsoft GraphRAG (100–1000× cost).
   - `ref_id` keeps the graph a *hint you verify* against live state.
3. **Multi-scope:** `scope ∈ team|member` + nullable `member_id` on `memory_entries`.
4. **chat→memory path:** a constrained agent tool proposes durable facts from chat → enqueues a
   `compile_memory` job (agent never writes facts directly — preserves compiler-only-writes).
   `source_kind='message'` already allowed; transcripts share this path.

### Retrieval cascade (security invariants held)
1. Fast path: corpus < ~200K tokens → load active facts with prompt caching, skip 2–4.
2. Vector candidates (pgvector top-k). *What.*
3. Graph expansion (recursive CTE, 1–2 hops over active edges). *How they connect.*
4. Read originals via `memory_citations` → live-read into multimodal model. *Verbatim.*
Throughout: compiler-only-writes; spotlight all untrusted text (extend to vision/transcript/link paths);
diff-card human-in-the-loop on `revised`/`invalidated`; verify action-bearing facts against live state;
close the exfiltration leg at the agent layer (no agent-rendered images/links to attacker hosts); PII
pass (Presidio) at ingest before embedding.

---

## Schema + pipeline deltas

- `documents.kind` enum → widen (`pdf|docx|xlsx|csv|pptx|image|code|transcript|link|text|unknown`); add
  `status='unsupported'`, `content_type_sniffed`, `disposition ∈ extracted|stored|quarantined`.
- `memory_versions`: add `change_type='invalidated'`; add **`embedding_model` + `embedding_dim`** (so a
  future gemini-embedding-2 migration is a phased backfill — the space is incompatible with -001); fix
  the stale `text-embedding-3-small` comment (code uses gemini-embedding-001).
- `memory_entries`: add `scope`, nullable `member_id`.
- New tables: `memory_entities`, `memory_edges` (Phase 1).
- `jobs.job_type`: add `sniff_route`, `live_read` (analyse-now), `resolve_entities`; fold `extract_graph`
  into the existing compile call.
- Split `compile_document` so "existing facts" context is **vector-top-k similar to the incoming doc**,
  not all facts (the single highest-value scale fix).
- Three-way UX over one parse: **analyse-now** (answer in chat, write nothing), **save-as-context**
  (current compile → diff card), **both** (analyse sync, then enqueue; "save" is a confirm on the diff card).

### Cost/scale notes
- Embedding is cheap & flat: gemini-embedding-001 = **$0.15/M tokens regardless of MRL dimension** → 1536
  is a *storage/RAM* knob, not an API-cost knob. Don't chase native 3072 unless recall is measurably hurt.
- Real cost driver = the all-facts-in-one-call compiler → top-k fixes it.
- Native PDF text from Gemini is free; only rasterized scanned pages cost image tokens → "extract local
  text, escalate to vision only on low density" is cost-optimal and beats a separate OCR engine.
- HNSW partial-index on active versions (have it) is right; pgvectorscale/StreamingDiskANN is the escape
  hatch if RAM-resident HNSW gets expensive.

---

## Phased path

**Phase 0 — Pilot (now): fix & harden, no graph.**
1. Kill "all active facts in one call" → **top-k vector retrieval** as compiler context.
2. Add **INVALIDATE/DELETE + supersession + entity-resolution/dedup**.
3. Add the **tiered ingestion router** (sniff → parse → multimodal → store-raw → quarantine); route
   scanned PDFs/images to Gemini multimodal.
4. Build the **chat→memory** path.
5. Store `embedding_model`/`dim` per version row.

**Phase 1 — Startup multi-team: add structure on real-data triggers.**
6. Add `memory_entities` + `memory_edges`; edges from the same compiler call; recursive-CTE 1-hop after
   vector top-k. **Trigger: teams routinely asking multi-hop coordination questions, or fact counts
   outgrowing flat top-k.**
7. Hybrid retrieval (BM25 + vector + 1-hop graph, RRF-fused) when volume warrants.
8. Render the **LLM-Wiki member-facing view** from the entity graph; make it the diff-card substrate.

**Phase 2 — Enterprise: scale the index, not the paradigm.**
9. pgvectorscale/StreamingDiskANN; phased re-embed to gemini-embedding-2 if recall justifies.
10. Dedicated graph DB only past ~10⁴ nodes/team or 3+ hop interactive traversal — defer until measured.

**Decide on real data:** top-k-alone vs graph (measure coordination-query failure rate); 1536 vs 3072
dims (measure recall); `k` and the entity-resolution merge threshold; the graph build trigger.

---

## Decision table

| Option | Verdict | When |
|---|---|---|
| Kill "all-facts-in-one-call" → top-k retrieval | **Do first** | Now |
| INVALIDATE/DELETE + dedup + entity-resolution | **Adopt now** | Now |
| Tiered ingestion router | **Adopt now** | Now |
| chat→memory (constrained tool → compile job) | **Adopt soon** | Now/next |
| Entity+edge graph (Postgres-native) | **Adopt later** | Phase 1, on real-data trigger |
| LLM-wiki member-facing view (rendered from DB) | **Adapt** | Phase 1 |
| llm-wiki lifecycle vocabulary (DELETE/supersession) | **Adopt now** | Now (= move #1) |
| Markdown-as-storage / graph DB / Apache AGE / MS GraphRAG / global chunk index | **Reject** | — |

---

## Key sources

Claude Code agentic search ([Vadim](https://vadim.blog/claude-code-no-indexing/)) ·
Claude RAG-for-Projects threshold ([Anthropic help](https://support.claude.com/en/articles/11473015-retrieval-augmented-generation-rag-for-projects)) ·
Claude memory ([Willison](https://simonwillison.net/2025/Sep/12/claude-memory/)) ·
Karpathy LLM Wiki ([gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)) + v2 ([gist](https://gist.github.com/rohitg00/2067ab416f7bbe447c1977edaaa681e2)) ·
LiCoMemory SOTA ([arXiv 2511.01448](https://arxiv.org/abs/2511.01448)) ·
Zep bi-temporal KG ([arXiv 2501.13956](https://arxiv.org/abs/2501.13956)) ·
Mem0 ([arXiv 2504.19413](https://arxiv.org/abs/2504.19413)) ·
structured-data retrieval ([Anyscale](https://docs.anyscale.com/rag/structured-data), [Smile](https://smile.eu/en/publications-and-events/retrieval-structured-data-precision-first-alternative-vector-only-rag-excel)) ·
cAST code chunking ([arXiv 2506.15655](https://arxiv.org/abs/2506.15655)) ·
Docling/parser benchmark ([Procycons](https://procycons.com/en/blogs/pdf-data-extraction-benchmark/)) ·
Magika ([repo](https://github.com/google/magika)) ·
Anthropic contextual retrieval / 200K threshold ([Anthropic](https://www.anthropic.com/engineering/contextual-retrieval)) ·
Supabase lacks Apache AGE ([discussion #13263](https://github.com/orgs/supabase/discussions/13263)) ·
lethal trifecta / memory poisoning ([Willison](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/)).
