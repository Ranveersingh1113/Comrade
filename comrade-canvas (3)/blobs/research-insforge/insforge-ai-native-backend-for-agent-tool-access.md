# InsForge assessment: study the patterns, don't adopt the platform

**Bottom line.** InsForge is a real, credible, Apache-2.0 backend-as-a-service for AI coding agents — but it is not the right move for this project. The team is on Supabase, and the cost of migrating (Postgres + pgvector + Realtime + Auth + Storage are all working together already) far outweighs any benefit. **The high-value finding is the other one: InsForge's MCP design philosophy — bundling schema, state, and permissions into single tool responses rather than forcing the agent to assemble context across multiple calls — is directly transferable to the planned custom platform MCP server, and should shape its design.** The "2x accuracy" claim is real in a narrow, self-published benchmark sense but should be treated as a design-direction signal, not as a head-to-head verdict on Supabase.

## What InsForge actually is

InsForge is an open-source (Apache 2.0) BaaS built as a TypeScript monorepo on **PostgreSQL + PostgREST + S3-compatible storage + Deno edge functions + JWT auth + WebSocket realtime + an OpenAI-compatible AI gateway**, deployed via Docker Compose either self-hosted or on their managed cloud [github](https://github.com/InsForge/InsForge) [docs.insforge](https://docs.insforge.dev/) [insforge](https://insforge.dev/blog/insforge-launch). Architecturally it is, in fact, **the same kind of stack as Supabase** — Postgres + PostgREST + storage + auth + functions. The novelty is not the substrate but the interface layer: an MCP server and a CLI with embedded "Agent Skills" (structured prompt-context bundles) designed for AI coding agents to provision and operate the backend without a human ever opening a dashboard.

This is important to internalize: **InsForge is not a different kind of database. It is Supabase's substrate with an MCP-first interface bolted on top.** That makes the design ideas portable.

## The "2x accuracy" claim: real benchmark, narrow scope

The claim originates from **MCPMark**, an open-source benchmark suite that **InsForge themselves built and run** [insforge](https://insforge.dev/blog/mcpmark-benchmark-results). It evaluates InsForge, Supabase MCP, and Postgres MCP across **21 real-world database tasks** (CRUD, RLS enforcement, joins, migrations, vector search, analytical reporting), each run 4 times under "Pass⁴" scoring (a task passes only if all 4 runs succeed).

| Version | Model | InsForge Pass⁴ | Supabase MCP Pass⁴ | InsForge tokens/run | Supabase tokens/run |
|---|---|---|---|---|---|
| MCPMark v1 (Dec 2025) | Claude Sonnet 4.5 | 47.6% | 28.6% | 8.2M | 11.6M |
| MCPMark v2 (Mar 2026) | Claude Sonnet 4.6 | 42.86% | 33.33% | 7.3M | 17.9M |

Source: [insforge](https://insforge.dev/blog/mcpmark-benchmark-results) [insforge](https://insforge.dev/blog/mcpmark-benchmark-results-v2)

A few things to read carefully:

- The headline "2x" comes from v1's 47.6 vs 28.6 (≈1.67×, marketed as "70% higher"). In **v2 the accuracy gap narrowed to 1.29×** (42.86 vs 33.33) — though the **token gap widened significantly** (InsForge used 59% fewer tokens than Supabase) [insforge](https://insforge.dev/blog/mcpmark-benchmark-results-v2).
- It is **InsForge's own benchmark on InsForge's own categorization of tasks**. There is no independent replication. One third-party reviewer made the obvious point: "47.6% vs 28.6% is huge gap, but with only 21 tasks, sample bias is fair critique" [reddit](https://www.reddit.com/r/SaaS/comments/1rp8us9/we_just_launched_insforge_20_an_open_source/).
- The methodology is **reproducible** (task set, model versions, scoring rule are all published), which is more than most vendor benchmarks offer [insforge](https://insforge.dev/blog/mcpmark-benchmark-results).
- An important second-order finding from v2: Supabase's token usage **increased 54% when moving from Sonnet 4.5 to 4.6**, while InsForge's decreased. The team's interpretation: "more capable models reason more extensively when backend context is incomplete" [insforge](https://insforge.dev/blog/mcpmark-benchmark-results-v2). If true, this is the more interesting result than the accuracy number — and it has direct implications for the team's Gemini 2.5 Flash/Pro choice.

**Treat the benchmark as a directional signal about MCP tool design, not a verdict.** The team should not pick a backend based on a 21-task vendor benchmark.

## The "fragmented context" critique: partially fair, overstated as marketing

InsForge's actual framing is: "Supabase's MCP primarily focuses on connection, but doesn't track or provide current backend structure. This means LLMs can make mistakes, creating duplicate or incorrect actions" [reddit](https://www.reddit.com/r/nocode/comments/1k3fkxl/i_built_insforge_llmnative_backend_that_makes/). They cite three concrete failure modes from the benchmark [insforge](https://insforge.dev/blog/mcpmark-benchmark-results):

1. **RLS visibility.** Supabase's `list_tables` returns table names and columns but not RLS policy state; an agent creating a new policy must run extra `execute_sql` queries first. On the `security__rls_business_access` task, Postgres MCP used 581K tokens / 23 turns and Supabase scored 25%, while InsForge used 296K tokens / 15 turns and scored 100%.
2. **Record counts.** Neither Supabase nor Postgres MCP exposes table row counts. On `employee_demographics_report`, an agent writing a LEFT JOIN didn't know that `salary` had 9.5× more rows than `employee`, producing wrong metrics (Supabase 75%, Postgres 50%, InsForge 100%).
3. **No upfront permissions.** Agents infer constraints and RLS from schema inspection, leading to "blind migrations" that fail on naming conflicts.

**The critique describes a real UX/efficiency problem, but the "connection only" framing is inaccurate.** Supabase MCP exposes **32 tools** including full schema introspection via `list_tables` (with columns, primary keys, foreign key constraints), `generate_types`, `run_sql`, `apply_migration`, `get_logs` [supabase](https://supabase.com/docs/guides/ai-tools/mcp) [supabase](https://github.com/supabase-community/supabase-mcp). It is not "connection only." The genuine difference is **how context is packaged** — Supabase forces the agent to call multiple tools (list_tables, then execute_sql for RLS, then another query for row counts), while InsForge returns a unified state snapshot in one call.

Importantly, **Supabase has already begun closing this gap**:
- PR #251 adds an RLS advisory to `list_tables` responses with security warnings and remediation SQL when RLS is disabled [chatforest](https://chatforest.com/reviews/supabase-mcp-server/).
- PR #258 added a server instructions field giving LLM clients context on safe interaction patterns [supabase](https://supabase.com/blog/supabase-agent-skills).
- They released **"Supabase Agent Skills"** — open-source instructions teaching agents how to build on Supabase correctly [supabase](https://supabase.com/blog/supabase-agent-skills). This is essentially Supabase adopting InsForge's CLI-Agent-Skills pattern.

The Supabase MCP server is converging on the same design principles. The gap is shrinking, not widening.

## What "auth knows DB permissions" means concretely

This is the most useful concept to lift, and it's worth a precise description. In InsForge, a single MCP call to `get-backend-metadata` returns approximately this in ~500 tokens  [docs.insforge](https://docs.insforge.dev/mcp-installation):

```json
{
  "auth": { "providers": ["google", "github"], "jwt_secret": "configured" },
  "tables": [
    { "name": "users", "columns": [...], "rls": "enabled", "policies": [...], "row_count": 1247 },
    { "name": "posts", "columns": [...], "rls": "enabled", "policies": [...], "row_count": 8911 }
  ],
  "storage": { "buckets": ["avatars", "documents"] },
  "ai": { "models": [{"id": "gpt-4o", "capabilities": ["chat", "vision"]}] },
  "hints": ["Use RPC for batch operations", "Storage accepts files up to 50MB"]
}
```

The auth section, the table schemas, the RLS state per table, the storage buckets, and policy/permission context all arrive together. An agent making a "create a posts table policy" decision sees the existing auth providers and the existing RLS state in the same payload it used to find the table. That eliminates the multi-call discovery phase where errors compound.

This is the pattern worth stealing.

## Maturity and credibility

- **License:** Apache 2.0, confirmed in the LICENSE file at repo root [github](https://github.com/InsForge/InsForge).
- **Stars:** Reported at **~10.8k as of early June 2026**, with reported growth from ~2.3k in Nov 2025 → ~5k after the v2.0 launch in early 2026. The growth curve is plausible for a YC company with a viral launch but the early numbers are second-hand; the live star count should be sanity-checked before quoting.
- **Activity:** Latest commit June 2, 2026 (one day before this writeup); 45 releases; latest stable v2.1.10 (May 29, 2026); 90 GitHub contributors [github](https://github.com/InsForge/InsForge).
- **Company:** Founded 2025; **Y Combinator P26 batch**; co-founded by Hang Huang (ex-Amazon PM, CEO) and Tony Yaowen Chang (ex-Databricks infra, CTO); San Francisco [ycombinator](https://www.ycombinator.com/companies/insforge) . Funding signals conflict: Tracxn lists "unfunded" as of May 2026, while a third-party site claims $1.5M pre-seed — single-sourced and unverified  [trend-hunt](https://trend-hunt.com/en/product/insforge).
- **Production usage:** Self-described case studies (Stanford Founders Demo Day project "MentorMates," named users "Zeabur" and "Peak Mojo") are vendor-sourced and unverified [trend-hunt](https://trend-hunt.com/en/product/insforge). The team itself says "almost 99% of operations on InsForge are executed by AI agents" with 500% database-creation growth — but this is from the launch announcement, not external telemetry [reddit](https://www.reddit.com/r/SaaS/comments/1rp8us9/we_just_launched_insforge_20_an_open_source/).
- **Known limitations practitioners cite:** free projects pause after 1 week inactivity; self-hosting setup is reported as "painful" for OAuth config; ecosystem depth and tutorials are sparse versus Supabase [linkstartai](https://www.linkstartai.com/en/agents/insforge) [edunavajas](https://edunavajas.com/en/blog/insforge-self-host/).

**Verdict on maturity:** It's a real project from a credible team — but it's **~8 months old** and Supabase has **7+ years of production operations and 99K+ stars** [trend-hunt](https://trend-hunt.com/en/product/insforge). A solo-dev pilot for student teams should not bet on the 8-month-old option when the 7-year-old option works.

## Should the team adopt InsForge? No.

The team's situation: Supabase is doing five jobs (Postgres, pgvector, Realtime, Auth, Storage), all of which are working together with consistent client SDKs and one auth/permissions model. The decision documents already commit to pgvector specifically for the embedding store  and to Supabase Realtime as the notification layer. Replacing Supabase with InsForge would:

- Require re-implementing the realtime, storage, and auth wiring with younger SDKs.
- Lose access to the Supabase Realtime → Postgres "write then ping" pattern that's already in the design.
- Trade a known operational profile for an unknown one in the middle of a pilot.
- Provide no benefit the team would notice, because **the only InsForge tool surface that matters to them — the MCP server design — is something they're already going to build themselves** for their platform actions (nudge member, create task, post to group chat).

There is no scenario where adoption is correct here.

## What to take from InsForge: design patterns for the custom platform MCP server

This is the actual deliverable. The custom MCP server the team is planning should adopt these patterns — most of them are not InsForge-specific, they're the emerging consensus from Supabase Agent Skills, Anthropic's MCP docs, and AWS/Speakeasy/Workato practitioner writing.

**1. Return state-with-result, not state-on-demand.** Every action tool should return the relevant updated state inline. `create_task(...)` should return not just `{ task_id }` but the task object plus the updated task list for that member and the member's current load. This is the InsForge `get-backend-metadata` pattern applied to platform actions [philschmid](https://www.philschmid.de/mcp-best-practices) [byteiota](https://byteiota.com/insforge-backend-platform-for-ai-coding-agents-tutorial-2026/).

**2. Bundle permissions/consent state into responses.** When the agent calls `propose_nudge(member_id, ...)`, the response should include whether that member has previously opted in to nudges, current cooldown state, and whether a similar nudge is already in the consent queue. The agent shouldn't need a separate `check_member_state` call [chatforest](https://chatforest.com/reviews/supabase-mcp-server/) [supabase](https://supabase.com/blog/supabase-agent-skills).

**3. Design outcome-oriented tools, not CRUD wrappers.** Don't expose `create_task`, `assign_task`, `set_deadline`, `notify_assignee` as four separate tools. Expose `delegate_task(member, description, deadline)` that does all four atomically and returns the composite result. InsForge's `get-backend-metadata` and Workato's "outcome bundling" both teach this [philschmid](https://www.philschmid.de/mcp-best-practices) [docs.workato](https://docs.workato.com/mcp/mcp-server-tool-design.html).

**4. Keep the tool count small — 5 to 15.** The cited practitioner consensus from GitHub Copilot and Speakeasy: "performance degrades sharply past 20 tools. The failure is not gradual; it's a cliff" [dev.to](https://dev.to/aws-heroes/mcp-tool-design-why-your-ai-agent-is-failing-and-how-to-fix-it-40fc). For the four AI pillars (monitor, surface gaps, engage with docs, take action), aim for ~10 high-value primitives, not 30 CRUD endpoints.

**5. Constrain argument types with `Literal` enums.** Instead of `nudge_member(member_id, config: dict)`, use `nudge_member(member_id, nudge_type: Literal["pending_task", "overdue_deadline", "idle_30days", "unopened_doc"])`. Constrained choices reduce hallucinated parameters [philschmid](https://www.philschmid.de/mcp-best-practices).

**6. Treat tool descriptions as agent context.** Tool docstrings, error messages, and parameter descriptions are read by the LLM at planning time. Be explicit about when to use, when not to use, what the response means, and what to do on error. Example error string: "Member is in quiet hours (22:00–08:00 local). Nudge queued for delivery at 08:00. Use force=true to override." [philschmid](https://www.philschmid.de/mcp-best-practices)

**7. Ship "Agent Skills" alongside the MCP server.** Both InsForge (as CLI Agent Skills) and Supabase (as the new "Supabase Agent Skills" release) ship documented workflows and example call sequences that the agent loads upfront [supabase](https://supabase.com/blog/supabase-agent-skills) [insforge](https://insforge.dev/blog/insforge-launch). For the ADK agent, write a few well-tested "playbooks" (e.g., "How to escalate a silent-member situation") into the system prompt or a retrievable skills doc. This is what reduces token use on Sonnet 4.6 — and would reduce it on Gemini 2.5 Flash too.

**8. Pin and disambiguate tool names.** Use `{domain}_{verb}_{object}` (e.g., `team_nudge_member`, `task_create`, `chat_post_message`). Distinct, prefixed names reduce confusion when both the platform MCP server and (eventually) the GitHub MCP server are mounted in the same agent context [philschmid](https://www.philschmid.de/mcp-best-practices).

**9. Build in reversibility hints.** The action consent flow on the canvas already requires user approval for visible actions. Augment tool responses with a `reversible: true/false` field and (where applicable) an `undo_token`. This makes the consent UI easier to write and gives the agent a way to recover from approved-then-regretted actions.

**10. Watch the security failure mode Supabase has and don't replicate it.** The most-cited real-world AI-agent-plus-Supabase pain is **prompt injection through user data when the agent has elevated privileges** — the documented case is an attacker hiding instructions in support tickets, the agent reading them with `service_role`, and exfiltrating tokens [pomerium](https://www.pomerium.com/blog/when-ai-has-root-lessons-from-the-supabase-mcp-data-leak) [generalanalysis](https://generalanalysis.com/blog/supabase-mcp-blog). The custom platform MCP server should call Supabase with a **scoped role** (not service_role) whose RLS policies actively constrain what the agent can read and write. The "AI as silent team member" model on the canvas already implies this — make sure the implementation enforces it.

## Adjacent context: is "Supabase + custom MCP" still the right shape in 2026?

For this product, yes. Convex has better real-time TypeScript ergonomics but no native pgvector and a smaller ecosystem [bertomill](https://bertomill.medium.com/convex-vs-supabase-which-backend-should-you-choose-in-2026-50d228c517de). Neon is Postgres-only and requires assembling auth/storage/realtime from elsewhere [bytebase](https://www.bytebase.com/blog/neon-vs-supabase/). Firebase is a heavier lift and weaker on portability [getfree](https://getfree.app/blog/supabase-vs-convex-ai-apps). The industry consensus call-out: "Supabase's primary advantage in 2026 is MCP integration + native Postgres. Convex MCP exists but is less mature. Firebase, Neon, Appwrite have no MCP" [metacto](https://www.metacto.com/blogs/supabase-competitors-alternatives-a-comprehensive-guide).

**The team is on the right substrate. The work to do is on the MCP layer above it.**

## Recommended next actions

1. **Do not adopt InsForge.** Keep Supabase. The migration cost is real and the benefit is captured by point 2 below.
2. **Adopt the `get-backend-metadata`-style pattern in the custom platform MCP server.** A single `get_project_state(project_id)` tool that returns members + active tasks + recent activity + outstanding consent items + RLS-scoped permissions context, in one ~500–1000 token payload, will measurably reduce the agent's discovery turns. This is the single most valuable design idea to lift.
3. **Adopt Supabase Agent Skills** (the official package) for the parts of the agent that interact with Supabase directly, and **write a small agent-skills doc for the custom MCP tools** so the ADK system prompt has them upfront. This both fixes the security defaults and saves tokens at planning time [supabase](https://supabase.com/blog/supabase-agent-skills).
4. **Audit the Supabase role the agent uses.** Make sure the platform MCP server connects to Supabase with a scoped role, not `service_role`. RLS only protects you if the role respects it [pomerium](https://www.pomerium.com/blog/when-ai-has-root-lessons-from-the-supabase-mcp-data-leak).
5. **Optionally — run the MCPMark methodology against the custom MCP server on a smaller task set** (5–10 platform-action tasks: "nudge a silent member," "create three tasks from this thread," etc.) before the pilot opens. Same Pass⁴ scoring. This gives you a concrete number for your own tools, calibrated on the actual workload, far more useful than InsForge's benchmark.

## Where more research would matter

Two areas would meaningfully strengthen these conclusions:

- **Independent replication of MCPMark.** No third party has re-run the InsForge benchmark on different task sets or different models. If the team wants to commit to specific design patterns based on the token-efficiency claim, running their own micro-benchmark on Gemini 2.5 Flash (their actual model) and a small task set drawn from the four AI pillars would be far more informative than any further desk research.
- **Verified production usage of InsForge.** The named users (Zeabur, Peak Mojo, MentorMates) are vendor-sourced. If the team ever revisits InsForge as an option, the question to answer is: does anyone with traffic and uptime requirements run it? Right now, the available evidence doesn't support a yes.