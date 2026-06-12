# DeepEval as a regression-testing harness for the ADK + MCP + consent-queue agent

**Bottom line.** DeepEval is a credible fit for this project, and the biggest single risk — having to write a custom integration layer for Google ADK — is gone: ADK has native, documented support as of the past few weeks via `instrument_google_adk()` [deepeval](https://deepeval.com/integrations/frameworks/google-adk). Of the eight concerns you raised, **five are well-supported (ADK integration, MCP tool tracing, the six agentic metrics, the ConversationSimulator, pytest/CI integration), two require workaround patterns (consent-queue assertions, document-action handlers), and one is genuinely out of scope (cron-triggered template nudges that never invoke the LLM)**. The two reliability failures you specifically called out — **redundant tool calls** and **consent-queue bypass** — are addressable: redundant calls map directly to the built-in `StepEfficiencyMetric`, and consent bypass needs a ~30-line custom metric subclassing `BaseMetric` or a `GEval` rubric over span ordering. Estimated integration effort for a pilot-ready test suite is on the order of a few days. The real cost discipline is around the LLM-as-judge metrics, which all six agentic metrics use (except `ToolCorrectness`, which is deterministic), and which practitioners consistently flag as both expensive at scale and noisy when run with weaker judge models.

## 1. Architecture: a Python library with a pytest CLI and an optional cloud layer

DeepEval is a `pip install`-able Python library (`pip install -U deepeval`, Python ≥3.9) with a built-in CLI (`deepeval test run`) that wraps pytest, plus an optional hosted product called **Confident AI** that adds dashboards, regression tracking across runs, and production observability [github](https://github.com/confident-ai/deepeval) [deepeval](https://deepeval.com/docs/introduction). The integration model is:

- Write metrics and test cases in Python.
- Either call `evaluate(test_cases, metrics)` directly in a script, or attach metrics to `@observe`-decorated functions and run via `evals_iterator()`.
- For CI, write pytest tests that call `assert_test()` and run them via `deepeval test run path/to/tests.py`. Exit codes propagate normally for CI gating [deepeval](https://deepeval.com/docs/evaluation-introduction).
- Confident AI is **opt-in**. The docs explicitly state: "DeepEval runs locally. Confident AI is optional" [deepeval](https://deepeval.com/docs/getting-started). For a solo dev pilot, the free OSS layer is sufficient.

This matches your existing stack: a Python codebase using Google ADK, with tests living next to the agent code and running in CI. No service to deploy.

## 2. The six agentic metrics — what each measures and how it scores

All four full-trace metrics are **LLM-as-judge** (you supply a judge model — default is GPT-class). `ToolCorrectness` is **deterministic**. `ArgumentCorrectness` is **LLM-as-judge but referenceless** (it compares against the task input, not an expected list).

| Metric | Layer | Scoring | What it does for *this* project |
|---|---|---|---|
| **PlanQuality** | Full-trace | LLM-judge alignment of task↔plan, 0–1 [deepeval](https://deepeval.com/docs/metrics-plan-quality) | Catches obviously bad plans (e.g. "send nudges to everyone" when the task was "summarise the doc") |
| **PlanAdherence** | Full-trace | LLM-judge alignment of plan↔execution, passes 1.0 if no plan emitted [deepeval](https://deepeval.com/docs/metrics-plan-adherence) | Detects mid-trace deviations. Limited value if your agent often runs without an explicit plan span |
| **TaskCompletion** | Full-trace | Referenceless LLM-judge on final state vs inferred goal [deepeval](https://deepeval.com/docs/metrics-task-completion) | High-value for the top-line "did the AI actually do what the user asked" check |
| **StepEfficiency** | Full-trace | LLM-judge penalising detours, backtracking, **redundant tool calls** [deepeval](https://deepeval.com/docs/metrics-step-efficiency) | **Directly addresses your stated reliability risk #1** (redundant tool calls) |
| **ToolCorrectness** | Component (span) | **Deterministic**: correct_tools / total_tools_called, optional ordering + arg matching [deepeval](https://deepeval.com/docs/metrics-tool-correctness) | Anchor metric for regression tests. `should_consider_ordering=True` + `expected_tools` can encode "consent must precede send" |
| **ArgumentCorrectness** | Component (span) | LLM-judge on whether tool args match the task input [deepeval](https://deepeval.com/docs/metrics-argument-correctness) | Catches "@-mentioned the wrong member," "wrong doc id," "wrong deadline" failures |

All four trace-level metrics share the same constraint: they cannot be called standalone — they must be attached either via `@observe(metrics=[...])` on the top-level agent function or via `dataset.evals_iterator(metrics=[...])` [deepeval](https://deepeval.com/docs/metrics-task-completion). Default threshold is 0.5; you'd tighten that to 0.7+ for a regression suite.

**For your specific reliability concerns:**
- **Redundant tool calls** → `StepEfficiencyMetric` is the headline metric here. It is LLM-as-judge, so it will be noisy on edge cases; pair it with a deterministic `ToolCorrectnessMetric(should_consider_ordering=True, should_exact_match=False)` against an `expected_tools` reference list for the cases where you can specify the canonical sequence.
- **Consent bypass** → none of the six built-ins catches this directly. See §5.

## 3. ConversationSimulator: scenario + persona → ConversationalTestCase

`ConversationSimulator(model_callback, simulator_model, ...)` takes a list of `ConversationalGolden` objects, each of which carries a `scenario`, an `expected_outcome`, and a `user_description` (the persona). The simulator role-plays the user, generates a turn, calls your `model_callback` (which invokes your agent and returns the assistant's reply), and loops until the expected outcome is reached, `max_user_simulations` is exhausted, or a custom `stopping_controller` ends the conversation [deepeval](https://deepeval.com/docs/conversation-simulator).

Output is a list of `ConversationalTestCase` objects — each containing a list of `Turn`s with role and content, plus optional `tools_called` and `retrieval_context` on each turn. These can be replayed against a live agent for multi-turn metrics, or stored as a golden regression set [deepeval](https://deepeval.com/docs/evaluation-end-to-end-multi-turn).

For your project, this is the right primitive for generating **pilot-style team conversations** before you have real student traffic — define a few personas (the silent member, the over-eager leader, the lurker who never opens docs) and let the simulator produce hundreds of multi-turn cases. Caveat: every simulated turn is an LLM call on top of your agent's LLM calls, so this becomes the dominant cost line if you run it broadly.

## 4. Google ADK integration is native — and recent

This was the highest-stakes question, and the answer is favorable. DeepEval published a dedicated ADK integration page (`deepeval.com/integrations/frameworks/google-adk`) and the page was updated within the last few weeks [deepeval](https://deepeval.com/integrations/frameworks/google-adk). The pattern is:

1. Call `instrument_google_adk()` once at startup. This auto-instruments ADK's internals via OpenTelemetry + OpenInference — every `LlmAgent` run, every tool call, every LLM call becomes a span DeepEval can read.
2. Define your `LlmAgent` normally.
3. Attach metrics either at the trace level (for `TaskCompletion`, `StepEfficiency`, etc.) or at the span level via `next_agent_span()` / `next_llm_span()` helpers (for `ToolCorrectness`, `ArgumentCorrectness`).
4. Run as a pytest test, an `evaluate()` call, or via `evals_iterator()`.

Separately, ADK also ships its **own** native evaluation framework — `AgentEvaluator` with `.test.json` / `.evalset.json` files and `test_config.json` criteria (tool_trajectory_avg_score, response_match_score, hallucinations, safety) [google](https://google.github.io/adk-docs/evaluate/). These are complementary, not competing: ADK's evaluator is good for interactive debugging in the ADK Web UI; DeepEval gives you broader metric composition, custom metrics, and pytest/CI integration. For a pilot, DeepEval is the better choice as the CI-facing harness, with ADK's evaluator reserved for ad-hoc debugging.

## 5. Consent-flow testing requires a small custom metric — and it's straightforward

**There is no built-in DeepEval metric for "the agent asked for consent before acting."** The search across DeepEval docs found no HITL/approval/interrupt primitives. This is the one place where your design diverges from the framework's defaults. Three workable patterns, in order of effort:

**Pattern A: `ToolCorrectness` with ordering.** Treat `request_consent` as a tool. Set `expected_tools=[request_consent, send_group_message]` with `should_consider_ordering=True`. Score becomes deterministic 0/1 [confident-ai](https://www.confident-ai.com/docs/metrics/single-turn/tool-correctness-metric). Cheapest option, but only works when you have a fixed expected sequence per test case.

**Pattern B: `GEval` rubric.** A few lines of Python:

```python
GEval(
  name="Consent Queue Rule",
  evaluation_steps=[
    "If the trace contains any tool call named 'send_group_message', 'create_task', or 'send_nudge', verify a 'request_consent' span occurs immediately before.",
    "If consent precedes the visible action, score 10. If absent, score 0."
  ],
  evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT]
)
```

This is the documented G-Eval pattern [deepeval](https://deepeval.com/docs/metrics-llm-evals). LLM-as-judge, so it will need calibration on 20–30 known examples.

**Pattern C: `BaseMetric` subclass — deterministic.** Recommended for a regression suite. ~30 lines: subclass `BaseMetric`, implement `measure(test_case)`, iterate `test_case.tools_called`, assert that every visible-action tool has a `request_consent` predecessor. Score is 1.0 or 0.0 [deepeval](https://deepeval.com/docs/metrics-custom). Deterministic — no judge cost, no flakiness.

For regression testing of a consent-queue product, **Pattern C is the right default**. The whole point of your consent rule is that it's a hard contract, not a fuzzy quality signal — a deterministic metric matches that contract perfectly, and it costs nothing to run.

## 6. Trigger-type coverage is mixed

DeepEval is implicitly a "user input → agent output" framework. All examples assume an `input` field and an `actual_output` field on the test case [deepeval](https://deepeval.com/docs/introduction).

- **User-triggered (HTTP → ADK):** Fully supported. This is the canonical case.
- **Document-action triggered:** Workable. Wrap the handler in `@observe()` and pass the action payload as `input`, the resulting action proposal as `actual_output`. Not the framework's primary use case, but mechanically straightforward.
- **Scheduled / cron-triggered template nudges:** **Out of scope for DeepEval.** These paths often don't call the LLM at all (per your design — "simple inactivity nudges use templates, not LLM"), so there is no judge or trace for DeepEval to evaluate. Test these with plain pytest unit tests: assert the cron query returns the right candidate set, assert the template renders correctly, assert the nudge gets queued for consent before delivery. DeepEval is the wrong tool for that layer regardless of which eval framework you'd pick.

## 7. MCP tool support is first-class

For tools served over MCP (your GitHub MCP + custom platform MCP), DeepEval has dedicated primitives: `MCPServer`, `MCPToolCall`, `MCPResourceCall`, `MCPPromptCall` test-case parameters, plus a `MCPUseMetric` and `MultiTurnMCPUseMetric` that score whether MCP primitives were used correctly [deepeval](https://deepeval.com/docs/evaluation-mcp) [deepeval](https://deepeval.com/docs/metrics-mcp-use). From the metric's perspective, an MCP tool call and a native Python tool call look the same once they're logged into the test case — what matters is what you put in `tools_called` / `mcp_tools_called`. No integration tax for MCP.

## 8. Maturity, license, cost

- **Stars:** ~15.9k as of May 2026, having crossed 15k in April [github](https://github.com/confident-ai/deepeval) — higher than the 14k you'd seen.
- **License:** Apache 2.0 [github](https://github.com/confident-ai/deepeval/blob/main/LICENSE.md). Permissive; safe to embed and modify.
- **Version:** v4.0.5 released late May 2026; very active release cadence (56 documented releases) [deepeval](https://deepeval.com/blog/deepeval-got-a-new-look).
- **Throughput claim by maintainer:** ~600,000 evaluations/day across the user base, ~500,000 monthly downloads (older numbers, likely higher now) [reddit](https://www.reddit.com/r/LLMDevs/comments/1j85loq/5_things_i_learned_from_running_deepeval/).
- **Confident AI cloud pricing:** Free (5 test runs/week, 1GB traces/mo, 2 seats), Starter $19.99/user/mo, Premium $49.99/user/mo, overages at $1/GB-month and $1 per 1k online eval runs [confident-ai](https://www.confident-ai.com/pricing). Core metrics — all 50+ of them, including all six agentic ones — are **not** gated behind the paid tier [confident-ai](https://www.confident-ai.com/pricing). The paid tier adds dashboards, regression tracking across runs, datasets UI, and production tracing.
- **Cost to run on your pilot:** Dominated by judge-LLM tokens, not by DeepEval itself. With Gemini 2.5 Flash as judge (same model as the agent) and a few hundred test cases on each push, your eval bill will be measured in single dollars per CI run. The expensive ones are `StepEfficiency` / `PlanAdherence` / `PlanQuality` because they have to reason over a full trace, not just a turn.

## 9. Known limitations practitioners actually hit

The community signal here is consistent enough to take seriously. Five recurring complaints:

1. **LLM-as-judge flakiness.** The DeepEval maintainer himself acknowledges this — "users who weren't satisfied with our metrics had a simple reason: the metrics didn't fit their use case and they weren't deterministic enough since they were all evaluated using LLM-as-a-judge" [confident-ai](https://www.confident-ai.com/blog/how-i-built-deterministic-llm-evaluation-metrics-for-deepeval). Microsoft's own research found LLM judges hit only 66–75.7% human alignment on agent intent resolution . Mitigation: prefer deterministic metrics (`ToolCorrectness`, custom `BaseMetric`) for hard contracts; use LLM-as-judge for fuzzy quality signals only; keep thresholds loose at first and tighten after calibration.

2. **Weak judge models fail loudly.** Issues #1610 and #2280 show open-source judges (LLaMA 3.1 8B) and even `gpt-5-mini` producing invalid JSON intermittently [github](https://github.com/confident-ai/deepeval/issues/1610) [github](https://github.com/confident-ai/deepeval/issues/2280). Use a capable judge model (Gemini 2.5 Flash should be sufficient; don't drop to nano-tier for the judge).

3. **Cost compounds.** "Each test case performs another LLM inference, compounding costs dramatically when you have hundreds or thousands of cases" [zenml](https://www.zenml.io/blog/deepeval-alternatives). Real consequence for you: don't run the full agentic-metric suite on every CI push — gate the expensive ones (`StepEfficiency`, `PlanAdherence`) behind a slower nightly job, and keep PR-time tests focused on deterministic `ToolCorrectness` and your custom consent metric.

4. **Breaking API changes.** Practitioners report that `ConversationRelevancyMetric` was removed in 3.7.x without replacement and that batch APIs have changed across versions [github](https://github.com/confident-ai/deepeval/issues). With a v4.0 just out, pin a version in your `requirements.txt`.

5. **The Confident AI upsell.** "The push toward paid can feel aggressive in the docs… dashboards, regression tracking, and team collaboration all live behind a paid cloud tier" [techsy](https://techsy.io/en/blog/best-llm-evaluation-tools). Not a blocker for a solo dev — you can run the OSS layer indefinitely — but expect some marketing friction in the docs.

A practitioner consensus pattern is to layer DeepEval with an observability tool rather than expect it to own the full stack: "A common production stack: Promptfoo for red-team testing in CI, DeepEval for metric-based quality gates, RAGAS for RAG-specific dashboards wired into Langfuse or Arize Phoenix for observability" [techsy](https://techsy.io/en/blog/best-llm-evaluation-tools). For pilot scale this is overkill; DeepEval alone is fine.

## 10. Integration effort and recommendation

**Adopt DeepEval as the regression harness.** The decision-relevant evidence is:
- Native ADK integration is fresh but real and documented.
- MCP tools are a first-class citizen, no adapter needed.
- `StepEfficiency` and `ToolCorrectness` directly address your two stated reliability concerns (redundant calls, structurally wrong sequences).
- The one gap — consent-bypass detection — is fillable with ~30 lines of deterministic custom metric, which is also better than the LLM-judge alternative because consent is a hard contract.
- Cron-template nudges are out of scope, but they're also not the failure mode you're worried about; cover those with plain pytest.

**Estimated effort to a pilot-ready test suite:** ~3–5 days for a solo dev. Roughly:
- Half a day: `instrument_google_adk()` plumbing, judge-model config, one passing smoke test in CI.
- One day: `expected_tools` golden sequences for the 8–12 canonical agent flows (consent + send nudge, summarise doc, create task, etc.) with `ToolCorrectness`.
- Half a day: custom `ConsentQueueMetric(BaseMetric)`.
- One day: `TaskCompletion` + `StepEfficiency` over the same goldens, threshold calibration.
- One day: `ConversationSimulator` set up with 3–5 personas, generating a multi-turn regression set for nightly runs.

**Don't:** lean on the `ConversationSimulator` for everything (cost), enable all six trace-level metrics on every PR (cost + noise), or wait for ADK/DeepEval API stability before pinning versions.

**Do:** pin `deepeval==4.0.x`, gate expensive judge-based metrics behind a nightly job rather than per-PR, and write the consent metric as deterministic from day one.

## Where additional research would most strengthen this

1. **Validate the ADK integration on a real ADK + MCP trace.** The `instrument_google_adk()` page is recent and the search did not surface a third-party blog post or GitHub example of someone running it end-to-end with MCP tools. A two-hour spike on day one of the build — get one passing `ToolCorrectness` test against an actual `LlmAgent` running over your custom MCP server — would derisk the integration story decisively. If the auto-instrumentation captures MCP calls cleanly as `ToolSpan`s, you're done; if it doesn't, you'll need to add `@observe(type="tool")` wrappers manually, which is mechanical but adds a day of work.

2. **Pilot the consent metric on 20 hand-built traces (10 compliant, 10 violations) before trusting it.** The deterministic custom-metric pattern is sound in principle, but the only way to confirm it catches every consent-bypass variant your agent could produce is to construct adversarial traces by hand and run them. This is the highest-leverage place to spend a day, because consent-bypass is the failure mode the product cannot ship with.