# The repo workspace: turning Comrade into a coding harness

**Decision, 2026-09-02.** Comrade hosts the checkouts itself and runs the work
locally. Not the team's GitHub Actions (Copilot's model), not a rented cloud
sandbox (Codex/Cursor's). That keeps the local-first prototyping loop and costs
no compute bill; it also means every containment question is ours to answer.

**What the agent works on is the team's connected repository — never Comrade's
own source tree.** That was the mistake in the first version of this plan: I
pointed the capability layer at Comrade's own directory, which is both useless
to a team and a self-modification hazard (the agent could edit the file listing
its own permissions). Making the root per-team dissolves that problem rather
than patching it: Comrade's source is simply not in the workspace.

---

## What the field does, and what we take from it

Researched 2026-09-02 across Copilot coding agent, Codex, Cursor cloud agents,
Devin, and the sandbox providers (E2B, Modal, Daytona). The pattern is
strikingly consistent.

| Practice | Them | Us |
|---|---|---|
| Repo reaches the agent by | clone into an isolated environment, per task | **clone into a per-team workspace we host** |
| Credential | GitHub App installation token, 1h, repo-scoped | PAT today → **App before a second team** |
| Network during the agent phase | off by default (Codex) or registry-only allowlist (Copilot) | **matters when `bash` lands, not before** — see below |
| Write-back | branch prefix + PR, never direct to main | **PR as a T2 consent action** |
| Inside the writable workspace | `.git`, `.codex`, `.agents` stay read-only | `.git` read-only, secrets denied |
| Executing repo code | always in a VM/container | **deferred behind containment** |

**Where we differ, deliberately.** Codex and Copilot make the agent
network-offline because their agent has a *shell* and could `curl` a secret out.
Comrade's agent has no shell yet, and its tools take no URLs — so today the
exfiltration path is not the network, it is the model putting a secret into its
reply, which is persisted as a message. That makes the path deny-list the right
first control and egress control the right control *the moment `bash` exists*.
Stated because I got this backwards once already and corrected too far the other
way.

**Where consent turns out to be exactly right.** Earlier I argued the consent
queue is the wrong mechanism, and that holds for a *local file write*: wrong
latency, wrong granularity, wrong axis. But the industry's unit of write-back is
not a file write, it is a **pull request** — team-visible, reviewable, needs a
human key, reversible. That is a T2 consent action by definition. So it is two
layers, not one: the capability budget governs edits inside the workspace, the
consent queue governs the push and the PR.

---

## Current status

### Exists and works

- `github_repos (team_id, repo_full_name, last_synced_at)` — records *which*
  repo per team. Nothing has ever cloned one.
- Webhook ingestion → `github_activity` → `repo_activity` tool. Event-driven;
  the repo has never been on disk.
- `agent/capability.py` — `ArgPolicy`, absolute secrets deny-list,
  resolve→contain→deny→allow ordering, shell-operator refusal, per-turn write
  cap. Built 2026-09-02. **Correct mechanism, wrong root.**
- `agent/permission_plugin.py` — reads `tool_args`; the ungated `sandbox`
  branch is closed; unscopable sandbox tools fail closed.
- Consent queue — propose → approve under RLS → execute exactly once via CAS
  under a separate role, tiers T0–T2, `AGENT_PROPOSABLE` bounding what the
  model may name.
- `pipeline/worker.py` — job queue with `lease_expires_at`, `MAX_ATTEMPTS = 3`,
  `PermanentJobError`, expired-lease reaping. **The only complete
  bounded-retry-with-escalation loop in the codebase, and the right home for
  clone/refresh.**
- Datamarking on every read path (2026-09-02).
- RLS across 43 migrations, four DB roles, two isolation audits.

### Does not exist

- Any clone or checkout. No workspace of any kind.
- A GitHub App. One long-lived `GITHUB_PAT` in `.env`, unscoped, no expiry.
- Any UI to connect a repo (`Setup.tsx` still says "COMING SOON").
- File tools, shell, execution.
- Egress control.
- Token/cost accounting; `AGENTS.md`.

### Impact map

| File | Change | Why it is not optional |
|---|---|---|
| `agent/capability.py` | `PROJECT_ROOT` constant → root passed per call | It currently points at Comrade's own tree. Everything else builds on this. |
| `agent/permission_plugin.py` | read the workspace root from `tool_context.state` | Server-bound, like `team_id`. The model must not name which team's files it stands in. |
| `agent/runtime.py` | put `workspace_root` in session state | Same seam `team_id` / `requester_id` already use. |
| `shared/workspace.py` (new) | derive, create, refresh, remove a team's checkout | The tenant boundary, in one place. |
| `pipeline/repo_sync.py` (new) | `clone_repo` job handler | Reuses the worker's leases and retry bounds rather than inventing a second loop. |
| `agent/tools.py`, `registry.py`, `agent.py` | the read tools, then the edit tool | Each declares its `ArgPolicy`. |
| `shared/consent.py` | `repo_open_pr` executor + T2 floor + `AGENT_PROPOSABLE` | The third entry that comment asked someone to argue for. This is the argument. |
| `server/app.py` | connect-repo endpoint | Nothing can reach a workspace that no team can create. |
| migration | `github_repos.workspace_state`, `last_cloned_at` | Clone status has to survive a restart, like every other job. |

---

## Where the earlier five tasks went

The plan before this one listed five. Two shipped; three moved, and one of
those changed character rather than merely position.

| # | Task | Now |
|---|---|---|
| 1 | Secrets deny-list, absolute | **Done** — `341517f`, 17 patterns, deny before allow, mutation-checked |
| 2 | Close the `sandbox` branch; chokepoint reads `tool_args` | **Done** — same commit; unscopable sandbox tools fail closed |
| 3 | Four tools: read, write/edit, glob/grep, bash | **Split across B / C / D** — one bullet became three because they need three different containment stories |
| 4 | `run_tests` — "nearly free" | **Re-priced. See below.** |
| 5 | `AGENTS.md` | **Two files, not one. See below.** |

### `run_tests` was mispriced, and it moves the sandbox onto the critical path

It was called nearly free because 575 tests already exist and the agent cannot
run one. That was true of a harness working in *Comrade's* repo. It works in
the *team's* repo, so `run_tests` means running **their** suite — arbitrary code
from a repository we do not control, on our host. Every system surveyed does
that inside a VM or container, without exception.

The playbook's claim is unchanged: self-verification is the highest-leverage
layer, and Layer 3's verify step is impossible without it. What changed is the
price. **The sandbox is therefore not a deferred nicety; it is the gate in front
of the harness's most valuable layer.** Phase D is not "later if we feel like
it" — it is the thing standing between this and a harness that can check its
own work.

### `AGENTS.md` is two files with two different readers

Conflated in the earlier plan, and separating them moves half of it forward:

- **Comrade's own `AGENTS.md`** — read by the coding agent building *Comrade*.
  Housekeeping: build/test/lint, the migration and RLS rules already written
  prose-style in `architecture.md`, and the drift the audit found (README says
  "four function tools", `architecture.md` says "five", there are thirteen;
  `HANDOFF.md` cites two `docs/` files that do not exist). Stays in **Phase E**.
- **The team's `AGENTS.md` / `CLAUDE.md`, inside their repo** — read by
  *Comrade's agent* when working on their code. Not housekeeping: it is how a
  team teaches Comrade their conventions without us building a settings screen,
  and it is the first thing `repo_read` should be pointed at. Moves to
  **Phase B**.

---

## Sequence

Ordered so that every phase leaves the system verifiably better and adds no
capability whose containment is not already in place.

### Phase A — the workspace exists and is isolated

**No new agent capability at all.** At the end of A the repos are on disk, per
team, and nothing can read them.

- **A1. Root becomes per-team and server-bound.** `capability.py` takes a root
  argument; `permission_plugin` reads it from session state; `runtime` puts it
  there. Path derivation is `WORKSPACES_ROOT / <validated uuid>` — a team id
  that is not a UUID never becomes a directory name.
- **A2. Tenant isolation sweep.** Team A's tools cannot reach team B's
  workspace, by the same standard as `test_product_read_paths.py`. This is an
  RLS-class boundary now, not a safety rail, and it gets RLS-class testing.
- **A3. Clone and refresh as a job.** `clone_repo` handler on the existing
  worker; `git fetch` at turn start; a token seam so the PAT is a stopgap the
  App can replace without a rewrite.
- **A4. Lifecycle.** Remove on repo disconnect, team archive, and last member
  out (`trg_membership_departure_effects` already knows when that happens). A
  disk cap, because unbounded checkouts on a shared host is how this becomes an
  outage.

### Phase B — the agent can read the repo

**No writes, no execution.** This is a real product capability on its own:
"why is this failing", "where is auth handled", answered against the actual code
rather than the compiled event record.

- **B1.** `repo_read`, `repo_glob`, `repo_grep`, scoped to the team workspace.
  `.git` denied, secrets denied.
- **B2.** Repo content is **datamarked**. It is stranger-written text by the
  same argument the GitHub extraction prompt already makes — a file in a repo
  anyone can open a PR against is not a trusted instruction source.
- **B3.** Registry entries and the instruction section naming the new surface.
- **B4.** Read the team's own guide file (`AGENTS.md` / `CLAUDE.md` at the repo
  root) into the turn, the way `wiki_section` already injects the page index.
  This is how a team states its conventions without us shipping a settings
  screen — and it is datamarked like everything else a human wrote, because a
  guide file in a repo strangers can PR against is not a trusted instruction
  source. It informs; it does not override Comrade's own rules.

### Phase C — the agent can propose changes

- **C1.** `repo_edit`, writing only inside the workspace, under the per-turn
  write cap, `.git` read-only.
- **C2.** Push + PR as a **T2 consent action**. Idempotent by construction: the
  branch name derives from `action_hash`, so a retried execute finds the branch
  and the open PR rather than opening a second one. That matters because the
  executor does network I/O inside a DB transaction — the one place the CAS
  guarantee cannot cover, so the action has to be safe to repeat instead.
- **C3.** Branch prefix `comrade/`, never a direct push to a default branch —
  the constraint Copilot enforces and the one that keeps review mandatory.

### Phase D — execution, behind containment

**On the critical path, not deferred.** Running a team's test suite is running
their code; everyone in the field does this in a VM or a container. Since
`run_tests` is the harness's highest-leverage layer and it lives behind this
gate, Phase D is what stands between Comrade and an agent that can verify its
own work.

- **D1. DONE** (`769ef49`) — `agent/sandbox.py`. `docker run` with no network,
  read-only rootfs, no capabilities, no-new-privileges, memory/CPU/pid caps,
  `.git` masked by a tmpfs, and output datamarked on the way back.
- **D2. DONE** (`769ef49`, `fa4acb9`) — ONE tool, `repo_run`, not `bash` plus
  `run_tests`: the second would be the first with a canned command and a guess
  about pytest-vs-jest, which the agent can settle with `repo_read`.
- **D3. DONE, for free** — `--network none` IS egress deny-by-default, and it
  is enforced by the kernel rather than by an allowlist we maintain.

Two host leaks were found by the machine falling over, not by a test:
a timed-out `docker run` kills the CLIENT and leaves the container spinning
(the test asserted the returned dict, not the side effect), and Git for
Windows' system-wide `core.fsmonitor` left one detached daemon per temporary
test repository — 418 of them, 15GB of commit charge, until an unrelated
pytest run died with a MemoryError pointing nowhere near git.

**Still open, and named rather than papered over:** the team's OWN
dependencies. No network means a repo with a `requirements.txt` gets a clear
"not installed". The fix is Codex's shape — a setup phase WITH network, then
network off for the run — which is a phase boundary, not a flag.

*Possible middle path, if D is too far off:* static analysers parse rather than
execute (`ruff`, `tsc`). Lower risk, not no risk — a parser bug or a config file
pointing at a plugin is still code. Worth taking only as a deliberate decision.

### Phase E — the harness layers still missing

- **E1. DONE** (`2391e1b`) — `AGENTS.md`, and `docs/**/*.md` un-ignored while
  the ~4MB of generated visual-check PNGs and HTML stay out. Drift fixed:
  HANDOFF said 4 tools and architecture.md said five, against an actual 19;
  two findings docs were cited in the reading list for months and never
  existed as files.
- **E2. DONE** (`97ca1fe`), and smaller than planned because the columns were
  already there. `agent_runs.input_tokens/output_tokens/cost_usd` had existed
  since the table did and nothing wrote them — so this was populating them,
  not adding them. Read from the ADK event's `usage_metadata`, accumulated
  across the empty-turn retries (a retried turn pays for its prompt each
  time), and written on the failed path too.

  Tokens are a fact, cost is a policy: counts are unconditional, `cost_usd`
  stays NULL until someone configures a rate, because zero is a price.

  The budget gained the dimension that tracks cost. A measured trivial turn
  is 5,125 input tokens before the member types a word, so "60 turns" was
  anywhere between 300K and several million — a turnstile, not a budget.

**`BudgetMulti`'s four dimensions were NOT built, on purpose.** There are two
real ones now (turns, tokens) and the check refuses on whichever binds first,
naming it. A four-dimension framework today would be one signal wearing four
hats; it is worth building when a third is genuinely in use.

---

## Gate: the GitHub App is required before a second team

Not a phase — a blocker. A PAT is one identity with one blast radius, and using
it to clone every team's repository means every team's checkout is reachable
with one credential that never expires. Acceptable while the only repo is our
own. **Unacceptable the moment a team that is not us connects one.**

The App also supplies the missing connect-repo flow, which is why it is worth
building as one slice rather than bolted on.

---

## What each phase buys, in the harness's terms

- **Security** — the workspace boundary is a tenant boundary of the same class
  as RLS, tested to the same standard. Execution stays behind containment. The
  token narrows from one unscoped PAT to a per-team hour-long grant.
- **Reliability** — clone and refresh reuse the worker's leases and bounded
  retries rather than a second ad-hoc loop; the PR action is idempotent by
  construction rather than by hope; workspace state survives restart because it
  is a row, not memory.
- **Efficiency** — review reuses the consent queue instead of a second approval
  system; the read phase ships before the write phase, so the product gets
  useful one phase earlier than it gets risky.
