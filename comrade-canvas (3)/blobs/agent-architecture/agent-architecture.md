# Agent architecture

Google ADK (Python) with one LlmAgent on Gemini 2.5 Flash. Tools via MCP: GitHub MCP server + custom platform MCP server. Three trigger types: user-triggered (HTTP → ADK), document action (user picks option → targeted ADK call), scheduled (cron → DB query → template nudge). GitHub context via repo graph — webhooks update only changed nodes, AI queries the graph rather than re-reading the repo. Memory: project-level in pgvector, user sessions backed by Supabase.

**Injection defense (two-layer):**
1. Input probe: all tool outputs (MCP responses, document content, GitHub payloads) are scanned and datamarked before entering agent context. HTML comments stripped from GitHub payloads. Spotlighting applied to all untrusted content.
2. Consent gate: runs as a separate process, no shared state with agent context. Cannot be corrupted by a poisoned document the agent read three steps earlier.

**Memory-as-hint:** the LLM-wiki is treated as a hint — the agent verifies key facts (who's committed recently, who has open tasks) against live Supabase/GitHub state before acting. Memory write authorization: only the document pipeline worker writes to project memory. Chat messages never write directly.

**Evaluation:** DeepEval with ToolCorrectness + custom ConsentQueueMetric (deterministic only, no LLM-as-judge). System monitoring via agent_runs table — log trigger, tool sequence, args, consent outcomes, wall time per step.