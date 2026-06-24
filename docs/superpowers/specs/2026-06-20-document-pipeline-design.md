# Document Pipeline — Design (v1)

**Status:** approved 2026-06-20. **Scope:** document → memory compilation only.
**Not a commitment:** ingestion + memory-management approaches are a starting
point; we will evaluate on real student data and revise (see "Evaluation").

## Goal

Turn an uploaded document into citative, versioned **facts** in project memory,
and post a glanceable diff card to the group — the "memory as a compiled
artifact" model. Onboarding uploads and later manual uploads. Chat→memory
recompile and GitHub sync are separate, later specs.

## Shape

```
upload → documents row + jobs row
      → worker (comrade_pipeline) claims job
      → parse (PyMuPDF4LLM / python-docx / WhatsApp regex)
      → spotlight (datamark untrusted text)
      → compile (Gemini: extract facts + diff vs existing) 
      → embed each fact (gemini-embedding-001, 1536)
      → atomic write: memory_entries/versions/citations + memory_compilations
      → post diff card to group chat
      → documents.status = ready, job done
```

Chunking is **not** a stage: Gemini's context holds a student doc; oversized
WhatsApp exports escalate to Gemini 2.5 Pro (a size-guard, not a splitter).
Map-reduce chunking is deferred until an input exceeds Pro's context (won't
happen at student-team scale).

## Validated technical choices (2026 research)

- **Parsing:** PyMuPDF4LLM (PDF → Markdown, reading order, tables) + python-docx
  (.docx) + regex (WhatsApp .txt). Docling/Marker = future upgrade for
  layout/OCR-heavy inputs; not needed for v1 text docs.
- **Embeddings:** `gemini-embedding-001`, MRL-truncated to **1536** (matches the
  existing `vector(1536)` column), `task_type=RETRIEVAL_DOCUMENT` for stored
  facts (`RETRIEVAL_QUERY` for future search). Single provider, key already set.
  Gemini Embedding 2 is preview + multimodal-overkill → revisit post-pilot.
- **Chunking:** recursive splitting (512 tok, 10–20% overlap) is the benchmarked
  default *if/when* needed; under compiled-facts it's mostly unused.
- **Injection defense:** datamarking (spaces→`^`) as baseline hygiene before any
  model sees content. Insufficient alone (RAG-poisoning is real), but the memory
  model already compensates: compiler-only writes, citative facts, visible diff,
  one-tap revert, and the agent verifies live state before acting.

## Compiler algorithm

**Chosen: A — single-call LLM compile.** Feed parsed+spotlighted doc + all
existing active facts to Gemini with structured JSON output. Each fact →
`{text, change_type: added|revised, revises_entry_id?, citations:[{source_kind,
source_id, excerpt}]}`. Code applies it: `added` → new entry+version; `revised`
→ new version on the entry, supersede prior (`is_active=false`, `valid_until`).
Embed each new/revised fact. Write the compilation run; post the diff card.

- **B — embedding-similarity + LLM confirm:** scale-up path past hundreds of
  facts. Deferred.
- **C — naive append:** rejected (accumulates duplicate/contradictory facts).

## Components (isolated, independently testable)

| File | Responsibility | Depends on |
|---|---|---|
| `pipeline/worker.py` | poll/claim jobs (`FOR UPDATE SKIP LOCKED`), dispatch by `job_type`, complete/fail with retries | `comrade_pipeline` role |
| `pipeline/parsers.py` | `parse_pdf` / `parse_docx` / `parse_whatsapp`; `spotlight()`; strip metadata/hidden text | pymupdf4llm, python-docx |
| `shared/embeddings.py` | `embed(texts, task_type) -> list[vector]` (1536) | google-genai (key set) |
| `pipeline/compiler.py` | orchestrate facts→diff→write `memory_*`+citations+run+card | parsers, embeddings, db |

## Data flow & boundaries

`documents` row + `jobs{job_type:'parse_document', payload:{document_id}}` →
worker loads content (Supabase Storage for files; inline for text/link kinds) →
parse → spotlight → `compile_document(team_id, document_id, text)` → atomic write
in one `team_session(PIPELINE)` transaction → `documents.status='ready'`, job
`done`. The storage fetch is the upload→pipeline seam; tests inject content
directly so storage wiring isn't a test dependency.

## Error handling & idempotency

Per-job try/except → `attempts++`, `last_error`, retry to N then `failed`. The
whole compile writes in **one transaction**, so partial failure rolls back and a
retry re-runs cleanly. LLM + embedding calls happen *before* the write
transaction (no external calls hold the row lock).

## Testing (deterministic-only, per project rule)

- **worker:** claim/complete; failing handler → `failed` + `last_error`;
  SKIP-LOCKED → no double-claim under concurrency.
- **parsers:** tiny PDF/docx/WhatsApp fixtures → expected text; spotlight
  datamarks; hidden-text/metadata stripped.
- **compiler:** mock the Gemini extraction + embedding (fixed fact list) and
  assert the diff/write logic — new vs revised, supersession, citations, run
  counts, card — deterministically. No LLM-as-judge.
- **smoke:** real doc → real Gemini → facts + card (script, like smoke_agent).

## Sub-steps

- **6a** worker spine · **6b** parsers + spotlight · **6c** `shared/embeddings.py`
  · **6d** compiler + enqueue-on-upload + smoke.
- 6a–6c need no API key; 6d uses the Gemini key.

## Evaluation (we expect to revise)

Measure on real student docs: fact extraction quality, dup/contradiction rate,
compile cost/latency, retrieval usefulness. If compiled-facts underperforms
chunk-RAG, or the single-call compiler doesn't scale, swap behind the clean
`compiler.py` / `embeddings.py` interfaces. Don't defend the first design.
