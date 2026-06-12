# Graphiti by Zep: is it a meaningful upgrade over pgvector + LLM-wiki for a 4-person student team companion?

**Bottom line: not for the pilot.** Graphiti is a serious, well-engineered temporal knowledge graph library — Apache-2.0, ~27K GitHub stars, an arXiv paper, real benchmark wins on long-conversation memory [arxiv](https://arxiv.org/abs/2501.13956) — but adopting it for a 4-person-team pilot on the planned Google ADK + Supabase + pgvector stack would impose **three concrete costs that the current scale does not justify**: a new graph database dependency (Graphiti cannot use Postgres [github](https://github.com/getzep/graphiti/blob/main/README.md)), ingestion latency of **60–200 seconds per long episode** that has forced other developers onto job queues ^[dev](https://dev.to/juandastic/beyond-rag-building-an-ai-companion-with-deep-memory-using-knowledge-graphs-2e6e "Beyond RAG: Building an AI Companion with \"Deep Memory\" using ..."), and a **documented async-event-loop conflict with Google ADK** that one developer could only resolve by isolating Graphiti in a separate subprocess [medium](https://medium.com/@saeedhajebi/building-ai-agents-with-knowledge-graph-memory-a-comprehensive-guide-to-graphiti-3b77e6084dec). At pilot volume (5–10 teams, hundreds-not-thousands of facts), the LLM-wiki + pgvector approach already in scope gets ~80% of the value at ~20% of the complexity. Graphiti becomes worth revisiting **only if** specific multi-hop or temporal-reasoning queries become a real blocker after launch, at which point the project memory layer can be re-platformed without losing user data. Detailed evidence follows.

## 1. How Graphiti works architecturally

**Graphiti is a Python library (`graphiti-core`, currently v0.29.1, released May 21, 2026) that runs in your process and writes to an external graph database.** [pypi](https://pypi.org/project/graphiti-core/) It does not bundle storage; you must provide one of four supported backends:

| Backend | Install | Hosting |
|---|---|---|
| **Neo4j 5.26+** (default) | `pip install graphiti-core` | Docker, Neo4j Desktop, AuraDB cloud |
| **FalkorDB 1.1.2+** | `pip install graphiti-core[falkordb]` | Redis-based, can be embedded |
| **Kuzu 0.11.2+** | `pip install graphiti-core[kuzu]` | Embedded, no server needed |
| **Amazon Neptune** | `pip install graphiti-core[neptune]` | AWS managed, requires OpenSearch Serverless for BM25 |
 
[pypi](https://pypi.org/project/graphiti-core/) [github](https://github.com/getzep/graphiti/blob/main/README.md)

**Postgres / pgvector is not a supported backend.** Graphiti could be deployed in the same environment as Supabase, but graph data lives in a separate database service, not in Postgres tables [github](https://github.com/getzep/graphiti/blob/main/README.md). This is the single most important finding for the team's decision: adopting Graphiti means **adding a graph DB to the stack**, not replacing pgvector. Kuzu is the lightest option (embedded), but it has open bugs around index lookups in example code [github](https://github.com/getzep/graphiti/issues/1112), and the official Docker image for Graphiti's MCP server only supports Neo4j [github](https://github.com/getzep/graphiti/issues/749).

Beyond the library, Graphiti ships an **experimental MCP server** (in `/mcp_server/`) that exposes `add_episode`, `search_facts`, `search_nodes`, `get_episodes`, and `delete_episode` as MCP tools over stdio or HTTP/SSE [github](https://github.com/getzep/graphiti/blob/main/mcp_server/README.md). This is relevant because the team's architecture already uses MCP — Graphiti could in principle slot in as another MCP server alongside the GitHub MCP server and the custom platform MCP server.

**Required runtime dependencies for self-hosting**: a graph DB, an LLM provider key (OpenAI, Anthropic, Gemini, Groq, or Azure OpenAI — defaults to OpenAI), and an embedder key. Graphiti **works best with LLM providers that support Structured Outputs** (OpenAI, Google Gemini); using Anthropic or smaller models is documented to cause schema validation failures [pypi](https://pypi.org/project/graphiti-core/). Gemini 2.5 Flash (the team's chosen workhorse) is supported.

## 2. The schema enforcement model — and a correction to the "10/10/10" claim

**Custom entity and edge types are defined as Pydantic `BaseModel` classes** passed to `add_episode()` via `entity_types` and `edge_types` dicts. An optional `edge_type_map` constrains which edge types are allowed between which entity-type pairs (e.g., `WORKS_ON` only between `User` and `Project`) [help.getzep](https://help.getzep.com/graphiti/core-concepts/custom-entity-and-edge-types). At ingestion time, the LLM classifies extracted entities against these types — it does not freely generate type labels when a schema is provided, though it can also operate schema-free.

**The "10 entity types / 10 edge types / 10 fields each" limit could not be verified.** No such hard cap appears in the README, PyPI page, official docs, or arXiv paper [github](https://github.com/getzep/graphiti/blob/main/README.md) [pypi](https://pypi.org/project/graphiti-core/). What does exist: GitHub issue #1211 reports a user with ~60 entity types splitting their data into multiple subgraphs [github](https://github.com/getzep/graphiti/issues/1211), and there is general practitioner guidance to keep schemas small for extraction quality. **Treat the 10/10/10 figure as folklore until shown otherwise** — but the design intent (small, curated schemas) is real.

There is also a **known caveat on schema persistence** that undercuts the value of custom types: issue #567 reports that custom entity types don't always persist their specific labels/properties to Neo4j — all custom entities get a generic `:Entity` label and some custom properties are ignored [github](https://github.com/getzep/graphiti/issues/567). If schema enforcement is the headline reason for adopting Graphiti, this is a yellow flag.

## 3. Temporal validity — the genuine differentiator

Graphiti implements a **bi-temporal model** with four timestamps on every edge [arxiv](https://arxiv.org/pdf/2501.13956) [neo4j](https://neo4j.com/blog/developer/graphiti-knowledge-graph-memory/) [github](https://github.com/getzep/graphiti/blob/main/README.md):

| Timestamp | Meaning |
|---|---|
| `created_at` | When the data was ingested into the system (system time, always present) |
| `valid_at` | When the fact became true in the real world (event time) |
| `invalid_at` | When the fact stopped being true in the real world |
| `expired_at` | When the system detected the fact was invalidated by a later contradiction |

When new information contradicts an existing edge, Graphiti **does not delete the old edge** — it sets `invalid_at` / `expired_at`, preserving full history [arxiv](https://arxiv.org/html/2501.13956v1). Default `search()` calls filter to currently valid facts (`invalid_at IS NULL`), and historical queries are answered by **manually filtering on `valid_at`/`invalid_at` timestamps** in the search parameters — there is no single "query as-of timestamp T" convenience method documented [help.getzep](https://help.getzep.com/graphiti/core-concepts/custom-entity-and-edge-types).

For the team's product, this model is genuinely well-matched to the four AI pillars — tracking when a user became active vs. went silent, when a deadline was set vs. moved, when a member joined or left the project. **This is the strongest single argument for Graphiti.** The question is whether you need it now or later.

## 4. Worked example: `User —WORKS_ON→ Project` and `User —USES_TECHNOLOGY→ Technology`

Practically, ingestion looks like this — adapted from official tutorials and the Neo4j blog post:

**Input episode (text or JSON):** *"Alice has been working on the Backend API project since January. She uses React on the frontend side too."*

**Extraction pipeline (LLM-driven, multiple calls per episode):**
1. Entity extraction → nodes: `Alice` (type `User`), `Backend API` (type `Project`), `React` (type `Technology`)
2. Entity resolution / deduplication against existing graph
3. Edge extraction → facts: `Alice WORKS_ON Backend API`, `Alice USES_TECHNOLOGY React`
4. Temporal extraction → `valid_at=2026-01-01`, `invalid_at=None`
5. Embedding of node summaries and edge fact text (e.g., 1024-dim vectors)
6. Edge invalidation: check whether any existing edges now contradict and mark them `invalid_at`

**Stored representation** (one edge): `EntityEdge(source_node_uuid="alice_uuid", target_node_uuid="backend_proj_uuid", fact="Alice works on the Backend API project", name="WORKS_ON", valid_at=datetime(2026,1,1), invalid_at=None, created_at=datetime(2026,1,1,10,0), expired_at=None, attributes={"role":"engineer"}, episodes=["ep_001"], fact_embedding=[...])` [github](https://github.com/getzep/graphiti/blob/main/README.md) [codepointer](https://codepointer.substack.com/p/agent-memory-systems-and-knowledge)

**Query (e.g., "Which team members work on backend projects and use React?"):** Graphiti's hybrid retriever runs three searches in parallel, fuses with **Reciprocal Rank Fusion**, and optionally reranks by graph distance from a focal node [help.getzep](https://help.getzep.com/graphiti/getting-started/overview) [neo4j](https://neo4j.com/blog/developer/graphiti-knowledge-graph-memory/):

1. **Vector cosine** on edge `fact_embedding` and node summaries
2. **BM25** keyword match on node names and edge facts
3. **Graph BFS** up to N hops from semantically similar starting nodes

Default retrieval latency is ~sub-200ms when graphs are small and hot . At scale, one production developer reported full retrieval taking 3–9 seconds on a graph with 10M nodes / 100M edges (BFS being "the killer" at 1–3 seconds) [reddit](https://www.reddit.com/r/singularity/comments/1pn803k/lessons_from_building_a_knowledge_graph_memory/). Pilot scale will be far below either threshold.

## 5. How it actually differs from pgvector + LLM-wiki for multi-hop queries

This is where the team needs to be honest about which queries it actually needs.

**Query types Graphiti handles well that flat vector RAG handles poorly:**

| Query | Graphiti approach | Vector RAG limitation |
|---|---|---|
| "Find team members who worked on a backend project AND use React" | Traverse `User —WORKS_ON→ Project` filtered by `Project.type=backend`, intersect with `User —USES_TECHNOLOGY→ React` | Retrieves chunks separately, relies on LLM to intersect; brittle |
| "What was Alice's role in January 2026?" (asked in May) | Filter edges by `valid_at <= 2026-01-31 AND (invalid_at IS NULL OR invalid_at > 2026-01-31)` | No temporal validity — stale and current facts coexist silently |
| "Who has committed code recently AND been silent in chat?" | Multi-edge filter on `created_at` and `last_activity_at` | Possible if you build it yourself, but pgvector alone doesn't provide it |
 
[atlan](https://atlan.com/know/vector-database-vs-knowledge-graph-agent-memory/) [machinelearningmastery](https://machinelearningmastery.com/vector-databases-vs-graph-rag-for-agent-memory-when-to-use-which/) [blog.getzep](https://blog.getzep.com/beyond-static-knowledge-graphs/)

On the **Zep team's own LongMemEval benchmark**, Zep (built on Graphiti) achieved 63.8% accuracy vs. 55.4% for a full-context baseline on gpt-4o-mini — a **15.2% accuracy improvement, 90% latency reduction (3.2s vs 31.3s), and 98.6% context-token reduction (1.6K vs 115K)** [arxiv](https://arxiv.org/html/2501.13956v1) [blog.getzep](https://blog.getzep.com/state-of-the-art-agent-memory/). Per-question-type gains were largest in temporal reasoning (+48.2%) and single-session preference recall (+77.7%), and modest in multi-session (+16.7%). These are real numbers from the published paper, but two caveats: (a) the benchmark uses ~115K-token conversations, which is much larger than a 4-person student project will produce in a week, and (b) Zep is the team that designed the benchmark.

A third-party head-to-head (a developer's own benchmark on a 14-message session) found **Graphiti consumed 2.25× more tokens than Mem0 (26.9K vs 11.9K)** and called out that "a single activity can trigger multiple LLM calls and embedding calls (node extraction, edge extraction, deduplication, etc.) — at scale this becomes very expensive," echoing GitHub issue #1193 [dev](https://dev.to/juandastic/i-benchmarked-graphiti-vs-mem0-the-hidden-cost-of-context-blindness-in-ai-memory-4le3) [github](https://github.com/getzep/graphiti/issues/1193). The Mem0 paper's claim of "600K tokens per conversation for Graphiti vs. 1,764 for Mem0" is disputed by Zep on test-configuration grounds, but the underlying point — that extraction is multi-call and unbounded — holds.

**For the team's specific use case**, the practitioner consensus is direct: "For 'Which team members have worked on a backend project?' (one hop), pgvector + hand-written graph schema + BM25 hybrid search gets 80–90% of the value with 20% of the complexity. Start with vector + RAG, add a simple custom knowledge graph schema only if specific multi-hop queries become a blocker" [machinelearningmastery](https://machinelearningmastery.com/vector-databases-vs-graph-rag-for-agent-memory-when-to-use-which/).

## 6. Deployment complexity in practice

Self-hosting Graphiti looks deceptively simple on the README but contains friction the team should price in:

- **Neo4j operational footprint**: Docker or AuraDB plus URI/user/password/database configuration [help.getzep](https://help.getzep.com/graphiti/configuration/neo-4-j-configuration). AuraDB has a free tier but it's small; Docker means another container alongside Supabase.
- **Async event-loop conflict with Google ADK** — the most concrete blocker for this team. A developer integrating Graphiti with Google ADK reports: "When graphiti-core is used within another sophisticated async framework like Google ADK, its internal async resource management (for HTTP and database connections) conflicts with the ADK's event loop. Standard in-process solutions (e.g., lazy client loading, `asyncio.to_thread`) were attempted and failed. The only robust solution is to isolate graphiti-core in its own process" [medium](https://medium.com/@saeedhajebi/building-ai-agents-with-knowledge-graph-memory-a-comprehensive-guide-to-graphiti-3b77e6084dec). This is not a minor inconvenience — it implies running Graphiti as a separate service (likely the MCP server or a FastAPI wrapper) and crossing a process boundary on every memory operation.
- **Ingestion latency for chat**: "Graph ingestion is slow. It takes anywhere from 60 to 200 seconds for Graphiti and Gemini to process a long conversation and update Neo4j" — the same developer ended up using Convex as a job queue and returning the UI immediately ^[dev](https://dev.to/juandastic/beyond-rag-building-an-ai-companion-with-deep-memory-using-knowledge-graphs-2e6e "Beyond RAG: Building an AI Companion with \"Deep Memory\" using ..."). Another user reports "correct answers only appear hours later after background graph processing completed" . For a real-time chat product, this means **every Graphiti write must be backgrounded** — the same `jobs` table pattern the team already has for document parsing would need to be extended.
- **Rate-limiting via `SEMAPHORE_LIMIT=10`** by default to avoid LLM 429 errors [github](https://github.com/getzep/graphiti) — adequate, but means concurrent ingestion is throttled.
- **Configuration bugs**: custom Neo4j database names break deduplication (issue #875), causing duplicate entities [github](https://github.com/getzep/graphiti/issues/875); NEO4J_DATABASE env var is ignored (issues #715, #1274) [github](https://github.com/getzep/graphiti/issues/715); FalkorDB unsupported in the official Docker image (issue #749) [github](https://github.com/getzep/graphiti/issues/749); nested-dict attributes throw Neo4j TypeErrors (issue #683) [github](https://github.com/getzep/graphiti/issues/683); custom entity-type labels/properties don't always persist (issue #567) [github](https://github.com/getzep/graphiti/issues/567); Pydantic validation failures on smaller/local models (issue #912) [github](https://github.com/getzep/graphiti/issues/912).

The MCP server is "experimental" by the team's own labeling [github](https://github.com/getzep/graphiti/blob/main/mcp_server/README.md) — usable, but not the polished interface the team would expect for a v1 platform memory layer.

## 7. Maturity, license, production readiness

- **License**: Apache-2.0 — fully permissive, no commercial-use restrictions [pypi](https://pypi.org/project/graphiti-core/).
- **Version**: v0.29.1 as of late May 2026 (still pre-1.0) [pypi](https://pypi.org/project/graphiti-core/).
- **GitHub**: ~27K stars, active commit cadence, sponsored by Zep (the commercial product) [github](https://github.com/getzep/graphiti).
- **Paper**: Published on arXiv in January 2025 (arXiv:2501.13956), peer-reviewed reception generally positive [arxiv](https://arxiv.org/abs/2501.13956).
- **Important**: **Zep Community Edition (the self-hosted Zep server) is deprecated**. Self-hosters now build directly on Graphiti core; the polished/managed experience is Zep Cloud only [vectorize](https://vectorize.io/articles/best-ai-agent-memory-systems). This matters because earlier evaluations of "Zep" may have been of the polished service, not the library the team would actually use.

One production user (r/LangChain) reports Zep is "production-ready and a more sophisticated product than mem0… had initial performance issues… It's really fast now"  — but this is the managed Cloud product, not self-hosted Graphiti. A separate review of the SaaS free tier was harsher: "Immature SaaS… don't expect it to work smoothly out of the box yet" [medium](https://medium.com/asymptotic-spaghetti-integration/from-beta-to-battle-tested-picking-between-letta-mem0-zep-for-ai-memory-6850ca8703d1). The honest summary: **Graphiti is alpha-to-beta on the open-source path with real production users; expect to encounter the bugs listed above**.

## 8. Limitations and caveats specific to this project

- **Postgres is not a backend** — adopting Graphiti adds a graph DB to a Supabase-only stack [github](https://github.com/getzep/graphiti/blob/main/README.md).
- **ADK async conflict requires subprocess/service isolation** [medium](https://medium.com/@saeedhajebi/building-ai-agents-with-knowledge-graph-memory-a-comprehensive-guide-to-graphiti-3b77e6084dec) — meaningfully complicates the team's current single-process ADK plan.
- **Long-conversation ingestion is 60–200s** ^[dev](https://dev.to/juandastic/beyond-rag-building-an-ai-companion-with-deep-memory-using-knowledge-graphs-2e6e "Beyond RAG: Building an AI Companion with \"Deep Memory\" using ...") — must be backgrounded, and the team's WhatsApp-export onboarding (potentially 100K–500K tokens) would be punishing to ingest as one episode.
- **Per-episode LLM cost is multi-call** (node extraction, edge extraction, dedup, edge invalidation) — could put pressure on the $10–40/month pilot budget if used for every chat message rather than for major project events [github](https://github.com/getzep/graphiti/issues/1193).
- **Custom entity-type persistence is buggy** (issue #567) — directly undermines the schema-enforcement value proposition [github](https://github.com/getzep/graphiti/issues/567).
- **No "query as-of timestamp" convenience** — historical queries require manual `valid_at`/`invalid_at` filtering.
- **Lock-in is real**: no standard export format for the temporal graph; migrating off later means writing a custom exporter and reconstructing temporal semantics elsewhere [atlan](https://atlan.com/know/best-ai-agent-memory-frameworks-2026/).

## 9. The verdict: where Graphiti fits in this project's roadmap

**Reject Graphiti for v1.** The case against is concrete, not stylistic:

1. The team's stack is Supabase + pgvector + ADK. Graphiti adds a graph database (Neo4j is the path of least resistance, but it's still another service to operate), requires subprocess/service isolation to coexist with ADK, and the integration would consume engineering time that the validation findings (88% pain match, 100% WhatsApp use) say should go into shipping the four AI pillars.
2. The pilot scale is **5–10 teams of 4 people**. The Zep benchmarks that demonstrate Graphiti's edge use **115K-token conversations** [arxiv](https://arxiv.org/html/2501.13956v1); a student team's *entire* week of chat will rarely approach that. The multi-hop queries actually needed at v1 ("who's gone quiet," "who hasn't opened this doc," "who owns this task") are mostly 1-hop and well-served by the LLM-wiki + pgvector design already in scope.
3. Ingestion latency is incompatible with chat-message-level memory updates. If every message had to round-trip through Graphiti extraction, the AI would feel slow and the LLM bill would compound. The team would end up batching anyway — at which point the LLM-wiki incremental update pattern (already chosen) accomplishes most of the goal without the graph.

**Plan to revisit Graphiti at one specific trigger**: when the project memory needs to answer queries like *"Which technologies has this team used across past projects? Have any members worked together before? Was Alice the project lead in January or has that changed?"* — i.e., when **multi-team, multi-project, multi-month historical reasoning** becomes a real feature requirement. That's a v2+ scope, not pilot scope.

**If you wanted to hedge today**, the cheapest hedge is to make the project memory layer thin and replaceable: a clean interface (`add_fact`, `query_facts`, `invalidate_fact`) backed by the pgvector + LLM-wiki implementation. This protects optionality without paying Graphiti's complexity tax up front. The four canvas blobs that describe the current memory model (`LLM stack`, `Agent architecture`, `Document pipeline`, `Real-time infrastructure`) already imply this kind of abstraction — making the interface explicit costs nothing.

**One thing the pilot should adopt from Graphiti's design, even without adopting Graphiti**: the bi-temporal pattern of `valid_at` / `invalid_at` on facts rather than deletion. If the LLM-wiki document stores facts with light validity metadata (when first observed, when contradicted), the team gets the most useful slice of Graphiti's temporal model for the cost of two extra fields in a JSON object — and it preserves the option to migrate to Graphiti later by populating the same fields on edges.

## Where additional research would most change the conclusion

1. **Run an actual two-day spike with Graphiti's MCP server + Gemini 2.5 Flash on a real student WhatsApp export.** Measure: ingestion time, total tokens consumed, extraction quality on student artifacts (which are messier than the SEC filings used in Zep's benchmarks). If ingestion of a 50K-token export costs more than $0.50 or takes more than 5 minutes, that confirms the recommendation. If it's cheap and accurate, reconsider. This is the same "spike on real student data" pattern the LLM-selection doc already recommends for the tier-3 model choice.
2. **Verify the ADK subprocess isolation requirement with a one-day prototype.** It's the single most actionable risk and is documented in only one source — if it doesn't reproduce, the integration story is materially better. If it does reproduce, the case against Graphiti for v1 strengthens further.