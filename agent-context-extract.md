# Extracted Context: Agentic Harness, Agent Memory, Agent Exposure

Source material distilled into reusable context. Three independent sections, one per link.

---

## Section 1 — Building an Advanced Agentic Harness

**Source:** https://data4sci.com/blog/building-an-advanced-agentic-harness

### Thesis

Turn a single LLM call into a reliable system that can **plan, act, recover, and prove it did the right thing**. Method: build small, testable primitives and wire them with a *thin* orchestrator. Composition over a monolithic loop.

### Running example

A "compare these cities" agent (population, timezone, narrative summary per city). Chosen because it yields 9 independent tool calls (3 cities x 3 attributes) + 1 aggregation step, mixes free lookups with expensive LLM calls, and has a deterministic pass/fail check (every city must appear in the output).

### Problem → solution map

| Problem | Solution |
|---|---|
| LLM hallucinates tool arguments | Pydantic-validated typed tools |
| Sequential execution is slow | Plan as a DAG, execute levels in parallel |
| Context window saturates | Tiered memory with a retrieval budget |
| Silent failures | Multi-tier verification (cheap deterministic → expensive LLM judge) |
| Unfocused prompting | Role-based agents: Planner / Worker / Critic |
| Runaway cost | Multi-dimensional budget with graceful degradation |
| Debugging opacity | Structured append-only trace |

### The seven primitives

**1. Pluggable brain (`LLMProvider`)**
Shared interface across backends: `complete(system, user, role) -> str` plus an async `acomplete()` wrapper. Enables provider swapping, a `MockProvider` for deterministic tests, and role-aware canned responses during development.

**2. Typed tools (`TypedTool` dataclass)**
Fields: `name`, `description`, `args_model` (Pydantic `BaseModel`), `fn` (callable), `cost_hint` (relative cost).
- Runtime validation before execution — fail fast.
- Generates JSON Schema matching Anthropic/OpenAI tool-use APIs.
- Field descriptions auto-populate the planner's tool catalog.
- `cost_hint` feeds budgeting. Example tiers: dictionary reads `0.1`, one-LLM-call summary `1.0`, token-heavy synthesis `2.0`.

**3. Plan as a DAG**
Planner emits JSON: node IDs, tool name, args, and a dependency list per node. Validate structurally *before* running — reject references to missing node IDs and circular dependencies so no tokens are wasted on a broken plan. `ready_nodes()` returns nodes whose dependencies are all satisfied.

**4. Parallel execution, bounded concurrency**
Level-synchronous DAG walker: compute the ready set, launch it concurrently with `asyncio.gather`, cap parallelism with `asyncio.Semaphore(MAX_CONCURRENT)`, wrap sync tools in `asyncio.to_thread`. The cap prevents rate-limit hits and cost spikes without rewriting tools as async-native.

```
while dag.not_done():
    ready = dag.ready_nodes()
    await asyncio.gather(*[run(n) for n in ready])
```

**5. Tiered memory**
- *Working*: current goal, plan summary, last few results.
- *Episodic*: outcomes of similar past runs (retrieved before semantic — past mistakes are more actionable).
- *Semantic*: background facts.
Assembly: retrieve by similarity to the current goal, enforce a hard character budget (~4000), truncate explicitly rather than lose silently. Similarity backends: Jaccard (free, paraphrase-blind) or `all-MiniLM-L6-v2` sentence embeddings (paraphrase-robust).
Principle: **context should be actively assembled, not passively accumulated.**

**6. Verification hierarchy**
- Tier 1 (deterministic, free): rule-based structural checks — required cities present, report shape valid; return actionable reason strings on failure.
- Tier 2 (LLM judge, expensive): only runs if Tier 1 passes; evaluates subjective quality.
Principle: **run the cheap tier first, only escalate survivors.**

**7. Role-based agents**
- *Planner*: input = goal + live tool schemas; output = DAG JSON only. Schemas are spliced into the prompt so it cannot invent tools.
- *Worker*: input = DAG; runs tools, records results; no judgment.
- *Critic*: input = goal + finished report; output = pass/fail + reasoning. Separated from generation to prevent self-grading.
Each role can be mocked and model-swapped independently.

### Cross-cutting machinery

**Multi-dimensional budget (`BudgetMulti`)**
Tracks tokens, tool calls, wall-clock seconds, and estimated USD simultaneously.

```
pressure() = max(
    tokens_used      / max_tokens,
    tool_calls_used  / max_tool_calls,
    elapsed_seconds  / max_wall_seconds,
    cost_usd         / max_cost_usd,
)
```

Degradation by pressure: `< 0.7` full pipeline incl. LLM judge; `> 0.9` skip the critic, deterministic checks only; `>= 1.0` halt and return partial results. Whichever dimension exhausts first sets the mode.

**Error classification and recovery**
| Class | Example | Recovery |
|---|---|---|
| Transient | rate limit, timeout | exponential backoff + jitter |
| Tool misuse | validation error | feed structured error back to the LLM |
| Missing information | unknown entity | re-plan with failure context (not blind retry), up to `max_replans` |
| Policy violation | — | halt immediately |

**Structured trace (flight recorder)**
Append-only event log, flat JSON-serializable dicts. Captures identity (step ID, parent ID → spawning plan node), semantics (role, action type), economics (latency, tokens, cost, pressure snapshot), outcome (critic verdict + reasoning). Lets you reconstruct order of events, per-step latency, which role burned budget, and whether pressure was rising before a failure. Shippable to files, OpenTelemetry/LangSmith, or straight to matplotlib.

**Thin orchestrator flow**
1. Build context from tiered memory.
2. Ask Planner for a DAG.
3. Execute via Worker; record trace, charge budget.
4. Check for execution failures.
5. Classify errors; re-plan if warranted.
6. Verify with pressure-aware degradation.
7. Store outcome in episodic memory.
8. Return `RunResult` (report, verdict, DAG, trace, budget).
The orchestrator only composes primitives — no orchestration logic of its own.

### Design principles

Fail fast · cheap before expensive · actively assemble context · composable primitives · graceful degradation · role separation for testability · explicit truncation · structured observability · recovery follows error class · a single pressure signal drives mode.

### Explicitly out of scope (future work)

Persistent vector storage (Chroma/Weaviate/pgvector) · tool-output sandboxing against prompt injection · human approval gates for irreversible actions · real token accounting from SDK metadata · a reliability-measurement eval harness.

---

## Section 2 — Memoryfield: Agent Memory as a File Format

**Source:** https://calpaterson.com/memoryfields.html

### Thesis

Treat agent memory as **data, not a pipeline**. Store it as an open, portable file format ("memoryfield") so agents keep agency and aren't locked inside a vendor platform. Invokes Brooks: *"Show me your tables ... and I won't usually need your flowcharts."*

### What a memoryfield is

1. Markdown pages, each with optional YAML frontmatter.
2. An optional SQLite vector index for semantic search.
3. Canonically distributed as a ZIP.

```
my-memories.memoryfield.zip
├── carbon-fibre-woks.md
├── finnish-bureaucracy-tips.md
├── ...
└── nomic-embed-text-v1.5.sqlite3
```

Per-page metadata: title, created timestamp, updated timestamp, UUID, summary.

### What it rejects

- **Proprietary memory**: vendors embed agents to move from API business to platform business; they mostly store facts *about the user*, not world knowledge.
- **Complex memory**: stacks needing pgvector + a Neo4j graph + their own LLM to decide what's rememberable. Confuses models, doesn't scale with model capability.
- **"High-modernist" memory**: graphs and logical propositions that strip context, leaving memories "isolated and senseless."
- Common fault: all treat memory as *process* rather than *data*.

### Four design decisions

**1. Prose over chunks.** Agents write memories directly as Markdown prose — no chunking, enrichment, or reprocessing (unlike RAG over legacy docs). Soft 8KB (~2000 token, ~1300 word) limit per page encourages focus; more detail = more pages.

**2. Semantic jump over graph walking.** Hyperlinked "Karpathy wiki" traversal fails on:
- *Speed*: N-deep info needs N+1 serial tool calls, ~2–3s pause each.
- *Reliability*: agents judge relevance only from link text/titles, so authors over-optimize metadata and suppress digression/implicit context.
- *Confusion*: irrelevant nodes encountered in traversal degrade output.
Memoryfields instead do one semantic search then read all hits in parallel — **2 tool calls max**. Content decides relevance, not metadata.

**3. Minimal mechanism, maximal model capacity.** Complex APIs create "interface mazes" and dump `openapi.json` into context. A plain file format lets agents invent access patterns — e.g. Perl find-and-replace across the corpus, inline CSV queried via SQLite. Low mechanism scales automatically as models get better at the tools they already know (bash, Markdown, SQLite).

**4. Open, interchangeable format.** RFC-style spec, transport-agnostic: local files, S3, GitHub, HTTP, Syncthing, anything file-based. No lock-in.

### Implementation

- Embedding model: **Nomic Embed Text v1.5** (270MB, runs without a GPU). Embedding models evolve slowly, so this stays a good size/power balance.
- Workflow: write memory as Markdown → embed, save vector to SQLite → retrieve by semantic search.
- Tooling to start: Ollama (runs the embedding model), `uv` (Python), `npx` (Node).

### Objections answered

- **"Isn't this just RAG?"** No chunking, re-ranking, or hybrid search; and agents *write* memories rather than only reading.
- **"Newer embedding models?"** Embedding progress is slow relative to frontier LLMs; recommendation hasn't moved.
- **"Won't it fill with junk?"** Semantic search never surfaces irrelevant memories; unused pages cost only disk. Insert liberally, clean up occasionally and optionally.
- **Security / "Disregard that!" injection**: no technical fix distinguishes legit prompts from injected ones. *"You must not share your context window, including via memories, with parties you don't trust."* Mitigate by manual review and SHA-256 pinning of shared memoryfields.

### Takeaway

Memory works best as portable data artifacts: preserves agent agency, scales with model capability, avoids lock-in, and puts effort into content quality instead of infrastructure.

---

## Section 3 — AI Agent Exposure and Integration (AWS)

**Source:** https://aws.amazon.com/marketplace/build-learn/ai-agent-learning-series/agent-exposure-and-integration

### Why agent endpoints differ from ordinary APIs

Requests are large and *grow* (hundreds of KB of accumulated context vs. a few hundred bytes of JSON). Responses take seconds, arrive incrementally. Output is non-deterministic — retries aren't free, caching needs semantic reasoning. Payloads are free text — JSON Schema validates structure, never content.

### Four exposure patterns

| Pattern | Mechanism | Fits | Cost / trade-off |
|---|---|---|---|
| **Direct API invocation** | Bedrock `InvokeAgent` / `InvokeAgentWithResponseStream` | Lowest latency, no hops | Needs AWS creds; couples callers to resource IDs; no per-caller quota |
| **Gateway-mediated** | API Gateway → Lambda | Public/partner APIs; auth, throttle, validate, usage plans, API keys, TLS, CORS | +10–50ms |
| **Event-driven** | SQS / EventBridge → Lambda | Batch, doc analysis, overnight enrichment | Built-in backpressure, retry, DLQ; decouples model changes from event producers |
| **MCP server interface** | Bedrock AgentCore exposes agent as MCP endpoint | Multi-agent, IDE/CLI tooling, discovery | MCP is protocol not policy — rate limit/quota/isolation still needed elsewhere |

REST vs HTTP API on the gateway: REST gives usage plans, JSON Schema validation, Cognito authorizers, WAF association; HTTP APIs are cheaper and lower latency but drop per-caller quota enforcement before model invocation. External developers also need a **developer portal** (self-service onboarding, analytics) — API Gateway enforces but doesn't provide this; partner tools (Tyk) add it.

### API surface design

**Base response contract**: POST to a path identifying agent (+ optional session); body carries message + caller context. Response must include **token usage for the turn** and an **explicit termination reason** (completed / output-limited / guardrail-blocked / tool-failed).

**Carrying large context** — payload ceilings cascade: API Gateway 10MB, Lambda 6MB sync / 256KB async, SQS & EventBridge 256KB.
- *Inline body*: fine to ~250KB. Beyond that, base64 inflates ~33%, JSON parsing burns Lambda duration, access logging multiplies CloudWatch cost.
- *Context by reference*: client uploads to S3 via a presigned URL (single key, tenant prefix, short expiry, content-length cap); passes only object key + content hash. Gateway never sees the payload. Best for logs, infra plans, build artifacts. Costs a second round trip.
- *Server-side session*: context accumulates server-side; caller posts only the new turn + session ID. Best over long conversations. Needs session affinity, state management, and visibility into when context exceeds the window.
- *Selection rule*: `<250KB` inline · `250KB–6MB` inline but log by reference · `>6MB` (or async `>256KB`) use reference · context grows per turn → server-side session regardless of current size.

### Protocols beyond REST

**gRPC / HTTP-2**: binary protobuf over multiplexed streams; native bidirectional streaming; less wire + serialization CPU for agent-to-agent traffic in a VPC. API Gateway can't proxy gRPC — terminate on ALB (gRPC target group → ECS/EKS) or VPC Lattice; Lambda isn't a gRPC target. Traffic management (throttle, retry, auth, transform, observability) must move elsewhere (Kong on EKS, VPC Lattice). Browsers need a gRPC-Web proxy.

**GraphQL via AppSync**: one endpoint, caller picks the response shape; great for composition (one round trip for agent answer + deployments + incidents + alert config). Per-field authz. WebSocket subscriptions push increments. Agent-specific risks: query cost is caller-controlled (denial-of-wallet via nested queries triggering multiple invocations); HTTP caching doesn't apply (all POST to one URL); need depth/complexity limits and count *agent invocations* against quota, not HTTP requests. Alternative: KrakenD assembles multi-backend responses declaratively, degrades per backend.

**Selection**: gRPC for internal service-to-service with native streaming · REST or GraphQL for public interfaces · GraphQL when clients genuinely need different shapes; prefer an aggregating gateway when everyone wants the same shape and the goal is just fewer round trips.

### Validation, idempotency, cancellation

- **Validation**: API Gateway JSON Schema rejects malformed requests before Lambda (saves invocation cost) but validates *structure only*. Tenant ID, system-prompt overrides, model selection, tool allowlists, KB filters must come from authenticated identity — **reject** requests carrying these fields (rejection signals probing; silence doesn't).
- **Idempotency**: require an idempotency key on any side-effecting request; store in DynamoDB with a conditional write; return the original result on repeat. Handle the in-progress branch or two concurrent retries both invoke the agent.
- **Cancellation**: API Gateway doesn't propagate client disconnects to Lambda — on the sync path nothing reacts. On WebSocket/gRPC, check a cancellation signal between chunks and stop consuming the model stream. For async jobs, expose an explicit cancel endpoint that writes a tombstone the worker checks between steps.

### API Gateway in production

**Auth**: IAM/SigV4 for service-to-service (no gateway latency, lands in CloudTrail, but tells you the role not the end-user) · Cognito user pools for user-facing (JWT in `Authorization`, gateway validates without invoking Lambda, claims reach the function via request context) · Lambda authorizers for everything else (return an IAM policy; **cache by key + TTL** or you pay a full invocation per request; keep TTL short if the policy depends on volatile state like token budgets or revoked entitlements).

**Usage plans / rate limiting**: sustained rate + burst + quota per API key; 429 returned without invoking Lambda or the model. **Requests/sec is a poor proxy for agent load** — 10 req/s of 500-token prompts vs 2 req/s of 100k-token prompts look identical to the gateway. Track **tokens per tenant** in the integration tier and enforce a token budget alongside the request quota. Publish per-tenant token consumption or you get recurring "we're under the rate limit, why did the bill move" tickets.

**WAF**: good for size constraints, rate-based rules (IP/header/JA4 fingerprint), Bot Control (agents are prime scraping targets), managed rule groups, geo. **Body-inspection ceiling**: WAF inspects the first 16KB (raisable to 32/48/64KB at cost) — 4–30% of a 200KB agent request passes unexamined; attackers pad benign text at the front. Sane response: set inspection to 64KB on agent endpoints, oversize handling = MATCH on a counting rule, and **stop expecting WAF for content control — it's volumetric and structural, not semantic**.

**Prompt injection**: WAF phrase rules ("ignore previous instructions") don't work — the space is unbounded and multilingual. Catch it in two places:
- *Structural* (integration function): strip/escape role markers, conversation delimiters, tool-result envelopes from caller text; wrap untrusted spans in explicit "this is data" boundaries.
- *Semantic* (Bedrock Guardrails): prompt-attack filter scores input **tagged as untrusted**. Tagging is critical — if the whole prompt is sent undifferentiated, the filter flags your own system instructions, teams lower the threshold, and the filter does nothing.
- Injection via retrieved KB chunks, MCP tool results, or prior session turns never crosses the gateway — screen retrieved content and tool output with guardrails too.

**Logging** — agent turns are large and sensitive. A moderate turn ≈ 40,000 tokens ≈ 160KB; 1M turns/month ≈ 160GB ingestion. Tiered design:
- *Tier 1 — metadata* (~1KB/turn): trace ID, tenant, principal, agent alias, model ID, per-phase latency, token counts, guardrail verdict, cache outcome, finish reason, error class. CloudWatch Logs, 13-month retention, Logs Insights. Answers latency/cost/error/capacity.
- *Tier 2 — hashed content* (~3KB/turn): SHA-256 of normalized prompt/response, retrieved chunk IDs, tool names, hashed tool args. S3 partitioned by date/tenant. Answers audit.
- *Tier 3 — sampled full fidelity*: complete request/response post-redaction; small % of traffic + all errors + all guardrail triggers. Separate KMS-encrypted S3 bucket, named-role access, 30-day expiry. Answers quality.
- Run Comprehend PII detection on the sampled path only. Default to logging *nothing* per field unless you've decided you need the value or its hash.

### Lambda integration tier

**One function vs many**: what differs per consumer (sync REST / SQS / EventBridge) is event shape, response contract, timeout (ms vs seconds vs 900s), memory, concurrency, IAM. What doesn't differ: resolving caller→tenant, assembling context, model selection, retry, guardrails, citation shaping, token accounting. → **thin per-consumer adapters over a shared core**. Test: changing agent behavior shouldn't force edits to multiple functions.

Single-function failure modes: one timeout (interactive inherits batch's 900s), one concurrency pool (batch backlog starves interactive), one execution role (unused permissions everywhere), one deployment (batch change breaks interactive).

**Adapter** = parse event → canonical request → call core → encode response contract → translate errors. Error translation must record terminal failures as terminal; only transient conditions go on the batch retry list.

**Packaging the shared core**:
- *Lambda layers*: fast config-only update across functions, but versioned by ARN suffix — easy to drift unnoticed.
- *Internal package*: built into each artifact via the build pipeline; dependency explicit in the lockfile; larger packages, rebuild+redeploy to update. Preferred for most teams (drift is a build-time fact, not a runtime surprise). Layers only for genuinely static deps.
- *Portkey AI (Marketplace)*: shared logic as a network hop with its own config — routing, fallback chains, load balancing, retry, token accounting change without a coordinated release. Cost: extra hop + failure mode. Pays off when consuming teams outnumber the people who understand the library.

Config that must differ per function: timeout, concurrency, memory, reserved concurrency, sync vs async, permissions. Split by **consumer characteristics, not by feature** (per-endpoint functions is the mistake). Split signals: branching on event shape at handler top; timeout set for the slowest consumer; execution role accumulating single-path permissions; one consumer's deploy needs another's regression test.

### Reliability

**Model failover**:
- *Retry with full-jitter exponential backoff* — synchronized retries reproduce the spike. Budget from the deadline, not a hardcoded count: 29s gateway ceiling − 8s model call = room for one retry.
- *Fallback to a smaller/faster model* when throughput is exhausted. Must: mark the response so the caller knows (silent degradation is worse than an error), and validate prompts on the fallback model.
- *Graceful degradation*: return a structured error — `{"_error_":"capacity_exhausted","_retryable_":true,"retryAfterSeconds":30}` — so callers back off intelligently. A generic 500 causes a harmful immediate retry.
- *Knowledge-base-only*: if the model is down but the KB works, return top matching chunks with sources, clearly labeled as search results not a synthesized answer.

**Multi-region** — what does *not* cross a region boundary: model availability (verify per-region in the pipeline), agent/alias IDs (regional — resolve from config keyed by region at startup, never hardcode), KB content (no cross-region replication — each region ingests its own; slower secondary = users silently get older docs), guardrail config (regional, versioned — assert version equality in a health check), provisioned throughput (per-region purchase — failover into an unprovisioned region hits on-demand throttling under the exact conditions you failed over for).
- *Session/context*: AgentCore Memory is regional (failed-over user starts fresh). Self-owned in DynamoDB global tables replicates sub-second — but sub-second ≠ zero; write a sequence number into the session, have the client send its believed current sequence, and on disagreement tell the user the conversation was interrupted.
- *Data residency*: an EU-only tenant contract makes US failover a breach. Model residency per tenant; some tenants get single-region service with lower availability by request. Needs residency-aware routing, not pure latency routing.
- *Escape hatches*: cross-region inference profiles (one request served from wherever in the geography has capacity; solves throughput + transient availability, not gateway/function outages; data leaves the source region). Active-passive secondary (colder, slower KB refresh, far cheaper than symmetric; tell the caller answers are from older sources during failover).

**Circuit breakers**: track failure rate over a rolling window; past threshold the circuit opens and calls fail fast; after cool-down, half-open lets one trial through, closes on success. State lives outside Lambda — DynamoDB (small write latency OK), not ElastiCache Redis if latency-sensitive. Break **per-dependency, per-tenant** (one tenant's bad MCP tool shouldn't open the circuit for everyone). Only transient failures (throttling) count toward opening — validation errors don't (backoff changes nothing).

### Sync vs async invocation

- **Sync**: caller waits, answer in the response body. Correct when response time is *reliably* under the gateway timeout and a human is waiting. Agent latency isn't normally distributed — a query with 3 tool calls + retrieval takes several times longer; size against **p99, not p50**; instrument time-to-first-token, tool duration, retrieval time, total separately. An 8s spinner feels bad regardless → streaming is near-mandatory for user-facing agents.
- **Async**: caller submits a job, gets an ID, polls or gets a callback. SQS between submission and processing; processing Lambda writes result to DynamoDB keyed by job ID; status endpoint reads the table. Durability: TTL on completed jobs; return a terminal state on failure (a job that never completes = a support ticket); cap polling (return a suggested interval, 429 impatient clients). Upgrade to Step Functions once the workflow exceeds one step — retries per error type, compensating actions on partial tool failure, execution history as an audit trail.
- **Selection**: sync for interactive latency with p99 under the ceiling; async for responses over the ceiling, batch, overnight, or unknown latency profiles. Payload ceiling: sync 6MB, async 256KB unless using context-by-reference.

### Streaming

Drops *perceived* latency from seconds to a few hundred ms; total duration unchanged.
- **SSE**: `Content-Type: text/event-stream`, events written as available. API Gateway **buffers** the integration response, so Lambda behind it can't stream — use a Lambda function URL (optionally behind CloudFront), `InvokeWithResponseStream`, a WebSocket API, or a container behind an ALB. Stream more than text: emit events for tool invocations ("searching runbook index"), citations as resolved, and a **terminal event** carrying finish reason + token usage. Once bytes are written the HTTP status is locked at 200 — failures must be expressed in the stream, and every client must require the terminal event before treating the response as complete.
- **WebSocket APIs**: persistent bidirectional connection for interrupt/correct/follow-up mid-response. Routes: `$connect` (auth + register), `$disconnect` (cleanup), message route (invoke, push chunks via the Management API by connection ID). Store resolved identity (tenant, principal, scopes) against the connection ID at `$connect` — don't re-derive per message, don't trust client-supplied identity. Limits: 2-hour max connection, 10-minute idle timeout — long conversations need reconnection + state restore. On disconnect during generation, check liveness between chunks and abandon so you don't pay for undelivered tokens.

### Caching agent responses

Ordinary HTTP caching assumes determinism, a complete key, cheap misses, and exact equality — agents violate all four (sampling; response depends on question + tenant + permissions + KB version + tool state + model + sampling params + prompt version + guardrail config; a miss costs a model call; paraphrases never byte-match).

**Exact caching first** (same problem, easier to see): never cache guardrail-flagged requests; include **KB version** in the key (every ingestion invalidates grounded answers — new version = clean namespace, old entries expire on TTL); enforce a **tenant** field in the key (never share a namespace across tenants).

**Semantic caching**: embed the incoming question, retrieve the nearest cached question, serve if above threshold. Failure mode with no HTTP equivalent — a *near miss is not a cache miss*; it returns a confident, fluent, wrong answer. Embedding similarity captures topic, not intent — these pairs score >0.95 but flip the answer: "Can I deploy to production?" vs "Can I *not* deploy?"; "enable MFA" vs "disable MFA"; "retention policy for staging" vs "production"; "roll back payments service" vs "payouts service". Make it safer by: deriving the threshold from your own labeled pairs (low-stakes ~0.93, operational >0.97 with a much lower hit rate); using below-threshold matches as *prompt-cache context* rather than substitutes; verifying near hits with a cheap fast model ("do these two questions have the same answer?") when stakes justify.

**Prompt caching** (no correctness risk — evaluate this first): caches the model's internal representation of a stable prompt prefix (system prompt + tool schemas + large repeating docs). Model still runs, response is fresh. Put stable content first in fixed order with cache checkpoints; variable content last. A single early variable token (timestamp, request ID) invalidates the whole prefix. Example: 2k system + 3k tool schemas + 12k stable docs = 17k input tokens removed per turn.

**Invalidation / poisoning**: include a sorted role list in the key so permission changes evict by key; include guardrail version. Semantic-cache poisoning — attacker seeds an answer to a question phrased near a common query. Defenses: never cache guardrail-flagged responses, never share namespaces across tenants, require a minimum number of independent occurrences before an entry is eligible for semantic matching, keep TTLs to hours not days.

**Measuring**: hit rate alone is misleading (lowering the threshold maximizes it — exactly what makes it dangerous). Track hit rate and false-hit rate together. Honest default: prompt caching always; exact caching for genuinely repeating stateless queries; semantic caching only after measuring real paraphrase traffic and only with a threshold from your own labeled data. Many production agents never run a semantic response cache — a legitimate outcome.

### Versioning and progressive rollout

- **Bedrock agent versions + aliases**: preparing an agent creates an immutable snapshot; an alias maps a stable name to one version. Integration tier references the alias. An alias points at exactly **one** version — no percentage canary at the alias level. Keep separate aliases for prod/staging/dev.
- Progressive rollout happens a layer up: **Lambda weighted aliases** (point at two published function versions with traffic weights; CodeDeploy automates the shift with CloudWatch-alarm rollback — closest to turnkey canary) · **routing in the integration tier** (pick the agent alias from a deterministic hash of tenant/session ID — must be sticky or conversations get incoherent) · **AgentCore A/B testing** (target-based routing across named endpoints, sticky by session, online eval scoring).
- Requirement across all: **stickiness by session ID** (hash the session, not the request).
- **API Gateway stages + rollback**: stages snapshot resource defs, integration config, usage plans, stage variables; deployment history = one-API-call rollback. Pair with CloudWatch alarms on 4xx/5xx, plus agent-specific alarms: guardrail-intervention rate (a prompt change tripping content policy shows up before error rates) and p99 latency (a new model version can be slower while entirely healthy).
- Behavioral regressions are invisible to error-rate monitoring (every request 200, fluent but unhelpful) → run an **evaluation suite against the canary in production**, compare to the stable version on the same inputs, gate promotion on the result.
- **Feature flags** (LaunchDarkly on Marketplace) answer *which* callers, not *how many* — target the tenants most likely to surface a regression (largest KB, longest questions, early-access opt-in) first, sticky per tenant, evaluate flags once at request start and carry through the turn. Especially valuable at the prompt/tool-config level, where changes are frequent and subtle regressions likely, and gives a rollback path with no agent version publish.

### Multi-tenant exposure

- **Isolation is enforced in the integration tier.** After auth, resolve tenant from token claims or the API-key usage-plan association; carry it into session attributes, KB filters, tool scope, audit records. **Golden rule: tenant from the authenticated principal, never from the request body** — a request containing a tenant field is rejected.
- **Per-tenant rate limiting + token budgets**: usage plans per API key handle request counts; track tokens per tenant in the integration tier (ElastiCache Redis atomic increment on a rolling window, <1ms). Decide up front what happens when a tenant exhausts its token budget mid-conversation: hard stop (users experience a break) vs degrade to a smaller model / reduced retrieval depth (alive at lower cost/quality). Both defensible; not choosing means finding out during an escalation.
- **Tenant knowledge bases**: Bedrock Knowledge Bases metadata filtering — tag documents with tenant at ingestion, assemble the filter server-side from the authenticated tenant. Never merge with a caller-supplied filter in the same expression — apply the caller filter as an additional constraint on the already-scoped result. Metadata filtering is a logical, not physical, control — strict-isolation auditors may require a separate KB/vector index per tenant; price that in rather than absorbing it.

### Observability

- Dimension every metric by **tenant** and **agent alias** (aggregate p99 hides one tenant having a bad time; the alias dimension puts canary next to stable on one graph).
- Core metrics: latency p50/p99/p99.9 measured per phase (gateway, model, retrieval, tool); token counts (input/output/per tenant); guardrail verdicts by type/severity; cache hit rate + false-hit rate + latency impact; model fallback frequency; error rates by class; tool-invocation success; cost per tenant/agent/model.
- **Distributed tracing** (X-Ray) across API Gateway → Lambda → Bedrock; annotate (indexed, filterable) for agent alias, tenant, session, tools invoked. Carry the trace ID through SQS message attributes / EventBridge detail / job records so async submission and completion aren't two unrelated traces.
- **Bedrock trace sensitivity**: agent traces expose intermediate reasoning — summarize into step descriptions for callers, route the raw trace to the tier-3 sink under the same access controls as full-fidelity content.
- One dashboard covering gateway + function + model + business metrics so a latency spike is interpretable against a simultaneous retrieval slowdown.

### Build-vs-buy (AWS Marketplace)

| Need | Tool |
|---|---|
| Model gateway (routing, fallback, load balancing, retry, token accounting) | Portkey AI |
| Developer portal (self-service keys, interactive docs, per-consumer analytics) | Tyk Technologies |
| Feature flags (caller-attribute targeting, sticky per tenant) | LaunchDarkly |
| Semantic caching as configuration | Portkey AI |
| Agent-shaped observability | Logz.io; Portkey AI (cost/latency per agent/model/consumer) |
| gRPC proxy on EKS, policies as k8s resources | Kong |
| Declarative multi-backend response aggregation, per-backend degradation | KrakenD |
| Multi-agent discovery | MCP server interfaces via Bedrock AgentCore |

### Recurring decision rules

- Payload: `<250KB` inline · `>6MB` or async `>256KB` by reference · grows per turn → server-side session.
- Retry budget from the deadline, not a fixed attempt count.
- Cheap deterministic checks before expensive LLM/model calls.
- Tenant, model selection, prompt overrides, tool allowlists: from authenticated identity only; reject them in the body.
- Cache correctness before cache hit rate; prompt caching before response caching; semantic response caching last, or never.
- Stickiness by session ID for every canary/rollout mechanism.
- WAF is volumetric/structural; prompt injection is caught structurally (integration function) and semantically (tagged guardrails).
