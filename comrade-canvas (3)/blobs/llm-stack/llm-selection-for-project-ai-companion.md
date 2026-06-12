# LLM stack for the student-team AI companion: recommended choices for the pilot

**Bottom line for a solo dev pilot (5–10 teams).** Use **Gemini 2.5 Flash** as your single workhorse model for tiers 1 and 2, escalate to **Gemini 2.5 Pro** (or Claude Opus 4.x if quality demands it) only for the rare tier 3 calls. Use **OpenAI text-embedding-3-small** with **Chroma** or **pgvector** for retrieval. **Do not adopt the OpenAI Assistants API** — it sunsets August 26, 2026, mid-pilot; build on either the Responses API or plain chat completions + a thin custom RAG. Multi-tier routing is not worth the wiring at this scale — the practitioner consensus is clear that you lack the production data to route well until you have real traffic.

The rest of this report walks through the evidence and the trade-offs.

## Recommended stack at a glance

| Layer | Recommendation | Why |
|---|---|---|
| Tier 1 + 2 default model | Gemini 2.5 Flash ($0.30 / $2.50 per 1M) | Cheap, 1M context, native PDF, multimodal — covers nudges, summaries, and message-thread analysis in one model |
| Tier 3 escalation | Gemini 2.5 Pro (~$1.25 / $10) or Claude Opus 4.x ($5 / $25) | Pro: cheapest path to 2M context for full WhatsApp exports. Opus: best structured extraction accuracy |
| Embeddings | OpenAI text-embedding-3-small ($0.02/1M) | "Good enough" at this scale; widely supported; trivial to swap |
| Vector store | Chroma (embedded) or pgvector if you're already on Postgres | Zero ops, swap later if needed |
| Orchestration | Plain chat completions / Responses API, no routing layer | LiteLLM/OpenRouter add complexity without ROI below ~10K req/day |

## Model pricing landscape, May 2026

Pricing has shifted significantly since the original GPT-4o / Claude 3.5 / Gemini 1.5 generation referenced in the brief. The current lineup:

**OpenAI** — GPT-4o has been superseded by GPT-4.1 and the GPT-5 family [fritz](https://fritz.ai/chatgpt-pricing/):

| Model | Input $/1M | Output $/1M | Context | Native PDF |
|---|---|---|---|---|
| GPT-5-nano | $0.05 | $0.40 | 128K | Limited |
| GPT-4.1-nano | $0.10 | $0.40 | 1M | Limited |
| GPT-5-mini | $0.25 | $2.00 | 128K–400K | Yes |
| GPT-4.1-mini | $0.40 | $1.60 | 1M | Yes |
| GPT-4.1 | $2.00 | $8.00 | 1M | Yes |
| GPT-5.4 | $2.50 | $15.00 | 1M | Yes |
| GPT-5.5 | $5.00 | $30.00 | 1M | Yes |

(Note: a secondary source quoted GPT-5.5 at $12.50 / $75 [developers.openai](https://developers.openai.com/api/docs/pricing) — the official pricing page should be checked before committing, but this only affects the high-end flagship which isn't recommended for the pilot.)

**Anthropic** — Claude 3.5 Haiku is the legacy budget option; the current cheap workhorse is Haiku 4.5 [platform.claude](https://platform.claude.com/docs/en/about-claude/models/overview):

| Model | Input $/1M | Output $/1M | Context |
|---|---|---|---|
| Claude Haiku 3.5 | $0.80 | $4.00 | 200K |
| Claude Haiku 4.5 | $1.00 | $5.00 | 200K |
| Claude Sonnet 4.6 | $3.00 | $15.00 | 1M |
| Claude Opus 4.7 | $5.00 | $25.00 | 1M |

Claude Opus 4.7 (April 2026) hit 87.6% on SWE-bench Verified and 98.2% field accuracy on structured invoice extraction at the same price as 4.6 [benchlm](https://benchlm.ai/blog/posts/claude-api-pricing).

**Google** — Gemini 1.5 Flash has been replaced; 2.0 Flash deprecates June 1, 2026 [devtk](https://devtk.ai/en/blog/gemini-api-pricing-guide-2026/):

| Model | Input $/1M | Output $/1M | Context |
|---|---|---|---|
| Gemini 2.5 Flash-Lite | $0.10 | $0.40 | 1M |
| Gemini 3.1 Flash-Lite | $0.25 | $1.50 | 1M |
| Gemini 2.5 Flash | $0.30 | $2.50 | 1M |
| Gemini 3 Flash | $0.50 | $3.00 | 1M |
| Gemini 2.5 Pro | $1.25 / $2.50* | $10 / $15* | 2M |
| Gemini 3.1 Pro | $2.00 / $4.00* | $12 / $18* | 2M |

*Pro models charge a higher rate above 200K input tokens [aipricing](https://www.aipricing.guru/google-ai-pricing/).

## Tier-by-tier model recommendation

### Tier 1 (private nudges, status messages, deadline reminders — dozens/day)

**Pick Gemini 2.5 Flash.** At $0.30 input / $2.50 output per million, a single nudge — say 500 input tokens (prior thread + system prompt) producing 80 output tokens — costs roughly **$0.00035**. Even at 500 calls/day across the whole pilot that's **~$5/month** [ai.google](https://ai.google.dev/gemini-api/docs/pricing).

You could go cheaper with **Gemini 2.5 Flash-Lite** ($0.10/$0.40) or **GPT-5-nano** ($0.05/$0.40), but the marginal savings on tier 1 are pennies. The harder design constraint here is that nudges must feel natural and not robotic — Flash-tier models handle short, gentle prose better than nano-tier models, which can sound terse or formulaic. Pay the small premium for output quality.

### Tier 2 (read thread, detect silent members, summarise project state — a few times/day)

**Same model — Gemini 2.5 Flash.** Its 1M-token context comfortably holds a multi-day chat thread plus relevant project memory. A summarisation call ingesting 20K tokens of conversation + project context costs ~$0.006 per call [tldl](https://www.tldl.io/resources/llm-api-pricing-2026). Even at 50 such calls/day that's ~$10/month.

The practitioner consensus is to default to GPT-4o-mini or its equivalent and only escalate when evaluation shows quality failure [aifreeapi](https://www.aifreeapi.com/en/posts/gpt-5-4-vs-gpt-5-4-mini). Holding the same model for tier 1 and tier 2 keeps prompt patterns, response style, and debugging surfaces consistent — which matters more than the few dollars you'd save splitting them.

### Tier 3 (PDF/docx/WhatsApp-export ingestion, multi-step action planning)

This is where you escalate. Two viable choices, depending on which trade-off matters more:

**For cost and context size: Gemini 2.5 Pro.** A 2M-token context window means a long WhatsApp export (typically 100K–500K tokens for a multi-month project) fits in a single call, alongside any uploaded project docs. Practical recall remains strong well past the 500K mark where other models degrade [openaitoolshub](https://www.openaitoolshub.org/en/blog/gemini-2-5-pro-review). At $1.25 input / $10 output below the 200K threshold, ingesting a 50-page PDF (~12,900 tokens via Gemini's efficient encoding [tokenmix](https://tokenmix.ai/blog/best-ai-for-document-processing)) is roughly $0.02 per extraction.

**For accuracy on structured extraction: Claude Opus 4.7** or **Sonnet 4.6**. Opus achieved 98.2% field accuracy on complex invoices vs. 95.8% for Gemini 3 in head-to-head testing [tokenmix](https://tokenmix.ai/blog/best-ai-for-document-processing). For your use case — extracting project goals, deadlines, members, and tasks from messy student docs — that 2–3 point accuracy edge can matter. Cost is ~3x Gemini's at $5/$25, but tier 3 calls run infrequently, so a per-team onboarding extraction might cost $0.10 on Opus vs $0.03 on Gemini Pro. At pilot scale this is rounding-error territory.

A reasonable starting position: **default tier 3 to Gemini 2.5 Pro, A/B against Claude Opus on a handful of real student documents during the first week**, and let the actual extraction quality on your data make the call.

## Is two-tier routing worth it for a solo dev?

**No, not yet — and the practitioner evidence on this is unusually clear.** Anthropic's own production guidance: "Routing early trades latency and cost for better task performance; only increasing complexity when needed." The starting recommendation is a single well-engineered prompt on one model [anthropic](https://www.anthropic.com/research/building-effective-agents). Reddit consensus from indie builders: "Don't route in pilot phase — you lack the data to optimize routes" [reddit](https://www.reddit.com/r/AI_Agents/comments/1qz6us7/how_much_are_you_guys_actually_burning_on_llms/).

**The real-world pilot-stage costs are very small.** A solo dev report from r/webdev describes going from $150/mo to $25–40/mo for a comparable product after caching and cheaper-model selection [reddit](https://www.reddit.com/r/webdev/comments/1rtquvn/solo_devs_using_llm_apis_how_much_are_you/). A customer-support chatbot handling 10,000 conversations/month costs around **$10/month on GPT-5 Mini** [cloudzero](https://www.cloudzero.com/blog/openai-pricing/). With 5–10 student teams, your call volume is roughly 1–2 orders of magnitude below that. **A single model for everything will likely cost you $5–$30/month across the pilot.** Routing complexity to save $10/month is exactly the premature optimization the literature warns against.

**Where routing actually pays off: 10K+ requests/day or compliance constraints.** Below that, gateway overhead and operational complexity outweigh savings [xenoss](https://xenoss.io/blog/openrouter-vs-litellm). For a pilot, sticking with a single SDK and a single mental model is worth more than the marginal token savings.

**The gotchas of premature routing that solo devs report:**
- **Tone drift across models.** Swapping mini → full mid-conversation produces noticeable shifts in voice and formatting that users notice [beam](https://beam.ai/agentic-insights/gpt-54-mini-and-nano-openai-just-validated-the-multi-model-agent-architecture). For an AI "companion" with a deliberately designed voice (per your AI voice guide), this is a real concern.
- **Debugging difficulty.** "Custom routing logic is way easier to debug when things go sideways" — i.e., than dynamic routing [reddit](https://www.reddit.com/r/LocalLLM/comments/1rtgp31/reducing_llm_token_costs_by_splitting_planning/).
- **Latency.** LLM-assisted classifier routers add ~50ms; semantic routers add 400+ms [aicoolies](https://aicoolies.com/comparisons/litellm-vs-openrouter). Negligible at your scale, but more complexity for no gain.

**The one routing pattern that is worth it from day one: a hard escalation rule, not a router.** Use Gemini 2.5 Flash by default, and have a single `if` statement in your tier-3 code path that calls Gemini 2.5 Pro (or Claude Opus). That's not routing — that's just picking the right model per code path. It costs nothing to set up and gives you 90% of the benefit.

**Cost guardrails matter more than routing.** Multiple practitioner reports describe agent loops running away — one developer reported $21 overnight on Haiku and $130 over a weekend from a single buggy loop; another reported $340 in a single day from an uncapped GPT-4 agent [reddit](https://www.reddit.com/r/AI_Agents/comments/1qz6us7/how_much_are_you_guys_actually_burning_on_llms/) [medium](https://medium.com/ai-analytics-diaries/ai-agents-the-complete-guide-i-built-12-agents-heres-what-actually-works-ce9e5de8ef37). Set hard per-day spend limits in the OpenAI/Google/Anthropic dashboards before writing any agent code. This catches ~95% of runaway cost incidents; prompt guardrails alone catch ~30% [ycombinator](https://news.ycombinator.com/item?id=47161209).

## Context windows: what matters for long WhatsApp exports

A WhatsApp export of a multi-month student project group is typically 50K–500K tokens. Three models can handle this comfortably:

- **Gemini 2.5 Pro / 3.1 Pro: 2M tokens.** Recall remains strong past 1M [openaitoolshub](https://www.openaitoolshub.org/en/blog/gemini-2-5-pro-review).
- **Claude Opus 4.7 and Sonnet 4.6: 1M tokens.** Strong, but practical recall starts degrading past ~500K.
- **GPT-4.1 family: 1M tokens.** Reportedly good for large codebases [pecollective](https://pecollective.com/tools/openai-api-pricing/).

For a one-shot onboarding ingestion of a long export, **Gemini 2.5 Pro is the cheapest path to genuine 1M+ context** — at $1.25/M input below 200K, $2.50/M above. A 300K-token WhatsApp export costs ~$0.75 to fully ingest with Pro.

However: for ongoing tier-2 calls, you should not be sending the whole WhatsApp history every time. The intended pattern is:
1. **Once at onboarding**, send the export to tier 3 (Gemini 2.5 Pro), extract structured project memory (members, goals, deadlines, key threads).
2. **Persist the extracted memory** in your project context store.
3. **Embed and chunk** the raw export for retrieval, so tier 2 calls only pull the relevant snippets.

This keeps tier 2 calls under 20K tokens of context and means a 1M context window is plenty for daily operation. The 2M is insurance for the onboarding step.

## OpenAI Assistants API vs Responses API vs custom RAG

**The Assistants API is being sunset on August 26, 2026.** OpenAI announced deprecation in August 2025 with a one-year migration window [developers.openai](https://developers.openai.com/api/docs/deprecations). It still functions today but receives no new features. **For a pilot launching in mid-2026 this is disqualifying** — you'd build on a foundation that breaks during the pilot.

**The replacement is the Responses API**, which OpenAI now positions as the default for new development [developers.openai](https://developers.openai.com/api/docs/guides/migrate-to-responses). It offers feature parity with Assistants plus new capabilities:
- `file_search` tool with managed vector stores
- `web_search`, code interpreter, computer use, MCP servers, function calling
- Conversation state via the separate **Conversations API** (`conversation_id`) or `previous_response_id` chaining
- 3% better SWE-bench than Chat Completions on the same prompts and 40–80% better cache utilization in OpenAI's internal evals [developers.openai](https://developers.openai.com/api/docs/guides/migrate-to-responses)

**Trade-off for your use case**: The Responses API's hosted `file_search` is fast to prototype with, but practitioner evals report a mean retrieval similarity of 2.41/5 across multi-document scenarios — single-document retrieval is much better [tonic](https://www.tonic.ai/blog/rag-evaluation-series-validating-openai-assistants-rag-performance). For a small pilot with a handful of docs per team, this is acceptable. There's also a per-file-search overhead — one report cites ~22K tokens added per file-search call even for small files [community.openai](https://community.openai.com/t/how-does-file-search-work-in-the-responses-api-and-what-is-its-pricing/1363656).

**Recommendation for a solo dev pilot**: **Plain chat completions + a thin custom RAG using Chroma or pgvector**. Reasons:

1. **You're not locked to OpenAI.** Gemini 2.5 Flash is the recommended default model — you can't use OpenAI's file_search with Gemini calls anyway.
2. **Custom RAG is straightforward at this scale.** Chunk docs → embed with text-embedding-3-small → store in Chroma → retrieve top-k on each query. This is ~50 lines of Python with LangChain or LlamaIndex helpers, or ~100 lines without.
3. **You retain full control** over chunk size, retrieval depth, and what gets injected into context. The Tonic eval found measurably higher retrieval accuracy with tuned custom RAG vs. Assistants File Search [thenewstack](https://thenewstack.io/openai-rag-vs-your-customized-rag-which-one-is-better/).
4. **Multiple practitioners report 40–60% cost reduction and 60% faster response times** migrating off Assistants API to chat completions + custom RAG [medium](https://medium.com/@gjasula/from-deprecated-to-optimized-a-production-migration-from-openai-assistants-api-to-chat-completions-21d784036644).

If you really want a managed file-search abstraction to skip the RAG plumbing entirely, use the **Responses API** (not Assistants) and pay the convenience tax. But for this product, a custom pipeline is the better default — and you already have the design constraint that the AI must reason over GitHub activity, WhatsApp exports, and project docs together, which is more flexible to coordinate from your own retrieval layer.

## Embedding models and vector store

**Use OpenAI text-embedding-3-small.** At your scale (5–10 teams, dozens of documents and chat threads each), retrieval quality differences between embedding models are negligible. Multiple sources confirm that for corpora under ~100K documents, dimension and model-quality differences have minimal practical impact [myengineeringpath](https://myengineeringpath.dev/tools/embeddings-comparison/).

Reference pricing for context [deploybase](https://deploybase.ai/articles/best-embedding-models) [tokenmix](https://tokenmix.ai/blog/text-embedding-models-comparison):

| Model | $/1M tokens | Dimensions | MTEB | Note |
|---|---|---|---|---|
| Google text-embedding-005 | $0.006 | 768 | 63.8 | Cheapest hosted |
| **OpenAI text-embedding-3-small** | **$0.02** | **1,536** | **62.3** | **Recommended default** |
| Voyage voyage-3-lite | $0.02 | 512 | 61.5 | 200M free tokens/yr |
| Jina embeddings-v3 | $0.02 | 1,024 | 65.5 | Best price-quality ratio |
| Voyage voyage-3-large | $0.06 | 1,024 | 67.1 | Highest retrieval scores |
| Cohere embed-v4 | $0.10 | 1,024 | 65.2 | Multimodal + multilingual |
| OpenAI text-embedding-3-large | $0.13 | 3,072 | 64.6 | 6.5x cost for +2.3 MTEB |

At pilot volume — even 100,000 tokens of embeddings per team per week — you're spending **under $1/month total on embeddings** with text-embedding-3-small.

**Vector store: start with Chroma (embedded).** Zero setup, pure Python API, fine up to ~1M vectors [pecollective](https://pecollective.com/tools/chroma-alternatives/). If you're already running Postgres for the platform's user/team data, use **pgvector** instead so you have one DB to manage — performance is adequate under 5M vectors [blog.elest](https://blog.elest.io/pgvector-vs-chromadb-when-to-extend-postgresql-and-when-to-go-dedicated/). Both are wire-compatible enough via LangChain/LlamaIndex that you can swap later without rewriting application code.

Avoid Pinecone for the pilot — $70+/month for production tier is unnecessary at this scale [encore](https://encore.dev/articles/best-vector-databases).

## Concrete pilot setup

If you implemented this exactly as recommended:

- **One LLM SDK** (Google's `google-generativeai`) plus the OpenAI SDK for embeddings only
- **Default chat model**: Gemini 2.5 Flash
- **Escalation model**: Gemini 2.5 Pro called from one `extract_from_document()` function and one `plan_multi_step_action()` function
- **Embeddings**: `text-embedding-3-small` called from one `embed()` function
- **Vector store**: Chroma running locally or in your app server
- **Hard spend cap**: $100/month in both Google AI Studio and OpenAI dashboards
- **Prompt caching enabled** on Gemini Flash for the system prompt (10% of normal rate on cached input — a 90% discount, and your system prompt will be the same across most calls) [benchlm](https://benchlm.ai/blog/posts/claude-api-pricing)

Estimated total pilot cost: **$10–$40/month** across 5–10 active teams.

## Where additional research would most strengthen this

1. **Actual extraction quality of Gemini 2.5 Pro vs. Claude Opus on real student documents** (messy syllabi, WhatsApp exports with code-switching and informal language). The published benchmarks are on invoices and code, not the kind of artifacts your users will upload. A two-day spike feeding identical student docs to both models would resolve this more confidently than any further desk research. Run this in week one of the build.

2. **GPT-5.4 and GPT-5.5 pricing** — sources conflicted between $2.50/$15 and $5/$30 for GPT-5.4. This doesn't affect the recommendation since the pilot won't lean on OpenAI's flagship tier, but verify directly at the OpenAI pricing page if you do consider it as a tier 3 alternative.