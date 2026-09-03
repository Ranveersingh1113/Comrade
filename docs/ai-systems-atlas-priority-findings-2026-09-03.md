# AI Systems Atlas assessment — P0/P1 decision log

Date: 2026-09-03  
Code baseline: `dc94d5a`, plus the uncommitted `agent/tools.py` batch-result fix.  
Source assessed: `D:\Downloads\ai-systems-atlas-teaching.md`.

This is a code-backed priority log, not an implementation specification. P0
means the foundation must be settled before Comrade grows into a cloud
multiplayer coding harness. P1 means important next-layer work that should be
built on the P0 contracts.

## Empirical baseline: four-person snake scenario

The scripted scenario in `sim/scenario.py` exercised the real HTTP endpoint,
member RLS, group and private messages, repository sync, agent tools, memory
compilation and consent proposals. It is a useful smoke/evaluation seed, not a
benchmark yet: it ran each task once, has no automatic outcome rubric, uses
mutable external state, and was assessed manually.

Observed strengths:

- Correct tool choice on the six turns and no invented test result.
- Correct responsibility and deadline extraction across several speakers.
- Useful cross-member reasoning in a private answer without exposing another
  private thread.
- The cited wiki retained most team decisions and repository constraints.
- The end-to-end run exposed a production-only worker registration failure
  that the large deterministic suite had missed; fixed in `dc94d5a`.

Observed failures and measurements:

- Six agent turns consumed 104,821 input and 2,368 output tokens. One task turn
  reached 32,641 input tokens. This is a warning about prompt/context growth,
  although this single trace does not by itself isolate the wiki index as the
  cause.
- Memory promoted requests to the agent ("create tasks...") into durable team
  facts and stored a game-start decision both atomically and as a duplicate
  combined fact. The write gate and consolidation quality need evaluation.
- The generated snake implementation contains a tail-collision edge-case bug.
  No verifier was run before the PR was proposed.
- The later question "Do the tests pass on what Comrade wrote?" could not truly
  inspect that proposed code. `repo_edit` changes the checkout during a turn,
  `repo_propose_pr` stores the captured patch in `consent_queue`, and the next
  turn's repository sync resets the checkout. The agent cannot resume the prior
  working state. Claude's suggestion that the later turn was simply one
  `repo_run` away is therefore incomplete: verification should have happened
  in the authoring turn, or the thread must retain a durable isolated workspace
  and artifact reference across turns.

These results directly strengthen P0.1 (durable thread/run state), P0.4
(repeatable outcome and trajectory evals), P1.1 (memory write quality) and P1.4
(durable cloud workspaces).

## P0 — foundation and safety

### P0.1 Replace room/private modes with real threads, participants and runs

**Atlas:** H-09 State Machine Agent, H-14 Long-running Agent, H-17 Steering a
Running Agent, X-02 Context Compaction, X-10 State Representation.

**Current code:** `messages` has only `thread_type = group|private`; group
messages have no conversation identity and private messages identify one
owner. `room_lock(team_id)` serializes the entire group room. Each turn creates
an in-memory ADK session and replays the latest messages. `agent_runs` records
an audit result, but is not a durable queued/resumable run state machine.

**Required direction:** introduce first-class threads, thread participants and
visibility, plus durable runs belonging to one thread. Serialize work per
thread, not per team. Runs need queued/running/waiting-for-user/completed/
failed/cancelled states, event/step records, steering, cancellation, leases,
and restart-safe resume. New messages in other threads should run in parallel.

**Why P0:** permissions, approvals, memory, files, skills and concurrent agent
work all need a stable thread identity. Adding them before this would encode
today's group/private limitation into every new subsystem.

### P0.2 Move approval into the thread without removing the authorization boundary

**Atlas:** H-12 Human-in-the-loop, H-18 Guardrail Middleware, S-05 Confused
Deputy, S-11 Unsafe Side Effects.

**Current code:** the model proposes a row in `consent_queue`; only the
requesting member can approve it; `comrade_executor` performs the approved
effect; the action hash binds the exact tool, team, requester and arguments.
This is a strong execution boundary, but the separate inbox interrupts the
conversation and the proposal has no thread identity.

**Required direction:** replace the consent inbox as the primary experience
with an inline tool-approval event in the initiating thread. Preserve the
durable proposal, exact-arguments hash, requester authorization, executor role,
compare-and-swap execution and audit record underneath. The agent run pauses at
`waiting_for_user`; approve, edit or reject becomes a thread event and resumes
that run. Low-risk read-only tools proceed automatically; risky effects require
an inline approval according to policy.

**Why P0:** eliminating the UI card is a product simplification. Eliminating
the durable gate would let prompt-injected tools perform side effects and would
be a security regression.

### P0.3 Harden cloud execution before promising local-harness parity

**Atlas:** S-04 Data Exfiltration, S-09 Credential Leakage, S-14 Sandbox
Anatomy, S-16 Skill & Plugin Supply Chain, G-16 Environment Bootstrap.

**Current code:** ordinary `repo_run` has no network, a read-only container
root, tmpfs, dropped capabilities and resource limits. The repository checkout
is still bind-mounted writable and the image currently runs using its default
user. Dependency setup deliberately runs as root with unrestricted bridge
network; the planned registry allowlist is documentation, not enforcement.
The dependency volume is later mounted read-only into runs.

**Required direction:** use disposable copy-on-write workspaces or isolated
worktrees, a non-root runtime, per-run CPU/memory/PID/disk/time quotas, explicit
network modes, an egress proxy/allowlist, secret brokering, process supervision,
preview-port routing, immutable base images and workspace cleanup. Dependency
builds are a distinct higher-risk sandbox phase. Never place platform
credentials inside either phase.

**Why P0:** “can run servers and everything a local harness can” creates
long-lived processes, network access and exposed ports. Those expand the trust
boundary and cannot safely be bolted onto the current one-command container.

### P0.4 Establish a harness evaluation system before the redesign

**Atlas:** E-01 through E-14, especially outcome, trajectory, regression,
adversarial, variance, cost/latency and reproducible environments.

**Current code:** Comrade has extensive deterministic backend/frontend/RLS/E2E
tests and a new four-person scenario. These verify product contracts and catch
bugs, but they do not measure whether the agent chose the right tools, completed
real tasks, retrieved the right memories, avoided unnecessary calls, or stayed
reliable across repeated stochastic runs and model changes.

**Required direction:** create versioned task suites from real Comrade jobs;
fresh pinned environments; deterministic outcome checks where possible;
trajectory assertions for tool/security invariants; human-calibrated judge
rubrics for qualitative output; adversarial prompt/memory/tool tests; repeated
runs with confidence intervals; and quality/cost/latency reported together.
Keep a held-out set and add production failure cases back into regression sets.

**Why P0:** without a baseline, a major thread/memory/tool redesign can become
more elegant while silently making the agent worse. Evaluation must exist
before, during and after the change.

### P0.5 Replace broad control-plane privilege and non-atomic budget checks

**Atlas:** H-15 Budgets & Stopping Rules, S-05 Confused Deputy, S-10 Privilege
Escalation.

**Current code:** cross-team job claims and several reconcilers use the table
owner `ADMIN` connection. Per-team work is scoped correctly afterward. Hourly
turn/token limits are aggregate prechecks, so concurrent turns can pass the
same remaining budget.

**Required direction:** add a narrowly granted control-plane/queue role and
atomic quota reservation/finalization. Enforce per-run wall-clock, model-call,
token, tool-call and sandbox-compute budgets.

## P1 — product capability built on the P0 contracts

### P1.1 Layer memory by thread, person, team and procedure

**Atlas:** X-01 through X-13, especially the four memories, write policies,
retrieval/reranking, compaction and cache-aware layout.

Keep the cited, versioned team wiki as semantic organizational memory. Add
thread working state and rolling summaries, opt-in personal notebook memory,
episodic run history and procedural skill memory. Give every write provenance,
scope, confidence/status and expiry/supersession rules. Retrieve broadly and
rerank narrowly; do not inject a forever-growing wiki index into every turn.
Pin constraints, decisions, current plan and unresolved approvals through
compaction.

### P1.2 Build one capability platform for tools, skills, MCP and connectors

**Atlas:** H-18 Guardrail Middleware, S-06 Tool Poisoning, S-16 Skill & Plugin
Supply Chain.

Treat these as four packages over one capability registry, not four unrelated
features. Every capability needs origin, version/digest, owner scope, declared
permissions, secret requirements, network policy, risk class, enablement scope
(user/team/thread), audit events and revocation. Slash commands should discover
and explicitly invoke skills; natural-language selection may follow policy.
MCP tools and connectors must still pass through Comrade's authorization,
budget, output-marking and sandbox boundaries. User-installed code is untrusted
supply-chain input, not an in-process Python import.

### P1.3 Add thread attachments by extending the existing document system

**Atlas:** S-02 Indirect Prompt Injection, S-08 Malicious Retrieved Content,
X-03 Retrieval, X-12 Observation Compression.

Comrade already uploads shared documents to a private Supabase bucket and
compiles them into team memory. Extend this with `thread_id`, message attachment
records, per-thread visibility and purpose-specific handling. An attachment can
be temporary turn context, a durable thread artifact, or explicitly promoted
to team knowledge; uploading it must not automatically publish it to the wiki.
Parse in isolation, preserve the original, mark extracted text as untrusted,
scan type/size, and provide citations back to the file.

### P1.4 Add durable cloud workspaces, background processes and previews

**Atlas:** H-13 Event-driven Agent, H-14 Long-running Agent, G-03 CI Feedback,
G-07 Worktree Parallelism, G-09 Checkpointing, G-16 Environment Bootstrap.

Build this after P0.3. A thread/run receives an isolated workspace derived from
a repository revision. Commands may be foreground jobs or supervised
background processes. Servers expose explicitly selected ports through
authenticated, expiring preview URLs. Persist process metadata and logs outside
the model context; return compressed observations and pointers. Add git
checkpoints/worktrees, CI webhook feedback and cleanup/idle-expiry policies.

### P1.5 Make the interaction mode explicit without repeated `@comrade`

Add a composer mode control: **Team** sends a human room message; **Agent** sends
the message to the agent in the current thread. Make the selected mode visually
obvious, keyboard accessible and sticky only at a carefully chosen scope. An
explicit mention should still invoke the agent from Team mode. Agent replies
remain attributed to Comrade, and human messages remain visible according to
the thread's participant policy.

## Open product decisions

1. Thread defaults and discoverability: team-public by default, private by
   default, or explicit choice at creation.
2. Whether selected-member threads are visible in the team's thread list to
   non-participants, even when their content is not readable.
3. Who may approve a side effect in a shared thread: the invoking member only,
   any participant with the capability, or a named owner.
4. Whether Agent composer mode is sticky per thread, per user, or resets after
   each send.
5. Which external capability is first: repository skills, remote MCP servers,
   OAuth connectors, or installable plugins.
6. Minimum cloud execution promise for the first release: finite commands and
   tests, supervised development servers, or full persistent environments.
