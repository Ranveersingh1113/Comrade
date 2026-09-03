# Comrade v2 — Phase 3: The Repository

> Parent plan: [`2026-08-29-comrade-v2.md`](2026-08-29-comrade-v2.md). Ledgers: [Phase 0](2026-08-29-phase-0-ledger.md), [Phase 1](2026-08-30-phase-1-ledger.md), [Phase 2](2026-08-30-phase-2-ledger.md).

**Goal:** Team memory compiled from real repository activity. This is the differentiator, it needs no sandbox, and §19-7 is explicit that it ships independently: *"if §17 slips, Comrade is still a working product."*

**Architecture:** Webhook → signature check → job queue → `github_activity` → the same two-stage compiler that already handles documents and chat, citing `github` sources. **Read-only.** No tokens are held, nothing is pushed, no GitHub App is required — repo webhooks with a shared secret are enough for ingestion. The App matters only for *writes* (§16.3), which this phase does not do.

**Tech Stack:** unchanged. `hmac` from the standard library — no new dependency.

---

## Global Constraints

Inherited. The six that bind this phase:

1. **RLS is the authorization layer.** New tables and columns need explicit grants; Supabase grants `anon` and `authenticated` full CRUD on everything by default.
2. **Migration policy:** index on an existing table → `CONCURRENTLY` in its own file; check constraint on an existing table → `NOT VALID` then `VALIDATE`.
3. **`spotlight()` before every LLM call** on attacker-controlled text. §16.6: *"Spotlighting must extend to the repo path before any repo-reading tool ships."*
4. **Compiled facts cite human-verified artifacts** — merged PR, review, human comment — **never the agent's own claims about its own work** (§22.3). See the provenance section below; this is the constraint most likely to be skipped.
5. **Feed the compiler PR titles, descriptions and review comments. Not raw diffs** (§16.6) — the compiler would emit noise facts.
6. **Every new tool gets a `REGISTRY` entry** in the same commit.

---

## 🔴 What is new and dangerous about this phase

Everything so far has sat behind a verified JWT. This phase adds **the first unauthenticated route in the codebase** and **the first attacker-controlled text that is not a document a member chose to upload**.

**The webhook is unauthenticated by necessity.** GitHub will not present a Supabase JWT. Its only credential is an HMAC-SHA256 signature over the raw body. That means:

- Signature verification is **mandatory and constant-time** (`hmac.compare_digest`). A `==` comparison here is a timing oracle.
- Verification runs on the **raw bytes**, before any parsing. Parse-then-verify lets an attacker feed the JSON parser unsigned input.
- No secret configured must **fail closed** — refuse every delivery, never accept unsigned. The project already made this mistake once in the opposite direction and fixed it in Phase 1 (`user_session` falling back to a BYPASSRLS connection).
- Replay protection uses the `X-GitHub-Delivery` id as `jobs.dedupe_key`, which the queue's existing partial unique index already enforces.

**The injection surface widens.** PR bodies, issue text and review comments on a public repo are written by strangers. They reach the compiler, which reaches an LLM. `spotlight()` must be applied on the repo path exactly as it already is for documents and chat — **before** any of this text meets a model.

---

## 🔴 The provenance rule, and why it is load-bearing now

§22.3, with evidence: Linear's 2026 report measured **~2,400 issues per week authored by agents against ~2,500 by people** — crossover imminent. A compiler ingesting issue and PR text is therefore already half-ingesting machine output.

Without a rule, Comrade compiles machine text back into machine context at scale, and the wiki slowly fills with an AI's claims about its own work, cited as if they were team decisions.

**The rule:** compile from human-verified artifacts only.

- ✅ A **merged** PR's title and description (a human merged it)
- ✅ A **human** review or review comment
- ✅ A **human-authored** issue or comment
- ❌ A bot-authored issue, PR or comment (GitHub marks these: `user.type == "Bot"`, and login suffixes like `[bot]`)
- ❌ An unmerged PR's description (nobody has verified it)
- ❌ Raw diffs, ever

Filter at **ingestion** and again at **compile**, and write a test for each. A `github_activity` row may still be *stored* for a bot event — `contribution_v` counting is a different question from wiki compilation — but it must not reach the compiler.

---

## Task order and why

| # | Task | Delivers | Rationale |
|---|---|---|---|
| 1 | Signed-webhook helper | F31 | Nothing else can be built safely first. Shared with Stripe later (§23.4-2) — *"worth one shared helper rather than two implementations."* |
| 2 | Schema deltas | F18 | Widen `node_type`, add the job type, fix grants. Everything downstream writes these rows. |
| 3 | Webhook → `github_activity` | F19 | The route plus the ingest job. |
| 4 | Repo facts → wiki | F20 | Needs spotlight extended first — that ordering is §16.6's, not mine. |
| 5 | Repo read tools | F21 | Last: the agent should only be able to read what the earlier tasks proved is safely stored. |

---

## Task 1: A signed-webhook helper that fails closed

**Files:**
- Create: `server/webhooks.py`
- Modify: `shared/config.py` (`github_webhook_secret: str = ""`)
- Create: `tests/test_webhooks.py`

**Interfaces produced:**
- `verify_signature(secret: str, raw_body: bytes, header: str | None) -> bool`
- `WebhookError` (or reuse an HTTP error shape — your call, state it)

- [ ] **Step 1: failing tests.** These are the security tests for the codebase's first unauthenticated door, so write them like it:
  - a correctly-signed body verifies;
  - a body altered by one byte does not;
  - a signature for a *different* secret does not;
  - a missing or malformed header does not (`None`, `""`, `"garbage"`, `"sha256="` with nothing after);
  - **an empty configured secret refuses everything** — including a body signed with the empty string, which is the trap: `hmac` will happily sign with `b""`;
  - the comparison uses `hmac.compare_digest`. Assert this by reading the source if you must, and say in your report how you verified it.

- [ ] **Step 2: run, confirm failure.**

- [ ] **Step 3: implement.** GitHub sends `X-Hub-Signature-256: sha256=<hex>` over the **raw request body**. Compute `hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()` and compare with `hmac.compare_digest`. Return `False` for anything unparseable rather than raising — a malformed header is a failed auth, not a server error.

- [ ] **Step 4: run, commit.** Keep this commit standalone: it is a security primitive, and a reviewer should be able to read it without a feature diff around it.

---

## Task 2: Schema for repository activity

**Files:**
- Create: `supabase/migrations/20260830170000_github_ingestion.sql`
- Create: `tests/test_migration_github.py`

- [ ] **Step 1: failing tests** — assert each change below exists, and that a member cannot INSERT into `github_activity`.

- [ ] **Step 2–3: the migration.**

```sql
-- node_type: webhooks deliver far more than commit/pr/merge (§16.5).
-- Widening a check constraint on an existing table: NOT VALID then VALIDATE.
alter table public.github_activity drop constraint if exists github_activity_node_type_check;
alter table public.github_activity add constraint github_activity_node_type_check
  check (node_type in ('commit','pr','merge','issue','review','comment','branch')) not valid;
alter table public.github_activity validate constraint github_activity_node_type_check;

-- The queue needs a name for the ingest work.
alter table public.jobs drop constraint if exists jobs_job_type_check;
alter table public.jobs add constraint jobs_job_type_check
  check (job_type in ('parse_document','embed','compile_memory','ingest_github')) not valid;
alter table public.jobs validate constraint jobs_job_type_check;
```

Then the grants, which are the part that actually matters:

- `comrade_pipeline` currently has **SELECT only** on `github_activity` and **nothing** on `github_repos`. It needs `insert` on `github_activity` and `select` on `github_repos` (to resolve a repo to a team), plus a `pl_github_repos` policy mirroring the existing `pl_github_activity`.
- **`authenticated` holds full CRUD on `github_activity` by Supabase default.** Members must not write repository history — it is a compiled record of what GitHub said, not something a person edits. Revoke everything but `select`. `au_github_activity_select` already exists and stays.

Add a lookup index on `github_activity (team_id, node_type, occurred_at)` — this table is new-and-empty in practice, so the index may be created in this migration without `CONCURRENTLY`; say so in a comment.

- [ ] **Step 4: run, commit.**

---

## Task 3: Webhook → `github_activity`

**Files:**
- Create: `pipeline/github.py`
- Modify: `server/app.py` (the unauthenticated route)
- Modify: `pipeline/worker.py` if handler registration needs it (it registers at import)
- Create: `tests/test_github_ingest.py`

- [ ] **Step 1: failing tests.** The route's security behaviour is the point:
  - an unsigned request is rejected (401/403 — pick one and be consistent);
  - a wrongly-signed request is rejected;
  - a correctly-signed request for a **registered** repo enqueues exactly one `ingest_github` job;
  - **the same delivery id twice enqueues once** — replay protection via `jobs.dedupe_key`;
  - a correctly-signed request for an **unregistered** repo is accepted and dropped without creating a job, and **without revealing whether the repo is known** (GitHub retries on non-2xx; a 404 here would also make the endpoint a registration oracle for anyone holding the secret);
  - the job handler writes a `github_activity` row with the right `node_type`, `author_github`, `occurred_at` and payload;
  - `author_user_id` is resolved through `profiles.github_username` when it matches, and left NULL when it does not — an unmapped author is not an error.

- [ ] **Step 2: run, confirm failure.**

- [ ] **Step 3: implement.**
  - The route reads the **raw body** (`await request.body()`), verifies, and only then parses.
  - Resolve the team: `repository.full_name` → `github_repos.repo_full_name` → `team_id`.
  - Enqueue with `dedupe_key = <X-GitHub-Delivery>`; the queue's existing partial unique index makes the retry a no-op.
  - The handler maps the event to a `node_type` and inserts. Support at least `push` (commit), `pull_request` (pr / merge), `issues` (issue), `issue_comment` (comment), `pull_request_review` (review). An event type you do not handle is **ignored deliberately**, not an error — GitHub sends many, and failing on the unknown ones would fill the queue with permanent failures.
  - **Store the useful subset of the payload, not the whole delivery.** A GitHub payload is large and contains a great deal you will never read; keep title, body, author, url, merged state, and the ids you need.

- [ ] **Step 4: run, commit.**

---

## Task 4: Repository activity becomes team memory

**Files:**
- Modify: `pipeline/github.py` (the compile path)
- Modify: `pipeline/parsers.py` if a repo-specific transcript formatter belongs there
- Create: `tests/test_github_compile.py`

This is where §22.3 and §16.6 bite. Model it on `pipeline/chat.py:compile_messages` — same two-stage shape, `source_kind='github'` citations. The citation trigger already handles `github` (verified: `trg_memory_citation_source_team`'s else-branch looks up `github_activity`), so no schema work is needed for citations.

- [ ] **Step 1: failing tests.** The provenance tests are the point of this task:
  - a **bot-authored** issue never reaches the compiler;
  - an **unmerged** PR's description never reaches the compiler;
  - a **merged** PR's title and description do;
  - a **human** review comment does;
  - a raw diff is never included in what is sent;
  - the text sent to the model is **spotlighted**;
  - a compiled fact carries a `github` citation pointing at the right `github_activity` row.

- [ ] **Step 2: run, confirm failure.**

- [ ] **Step 3: implement.** A `compile_github_activity(team_id, ...)` that gathers eligible rows since a watermark, formats them into a numbered transcript (as `format_transcript` does for chat, so `source_index` can map a candidate back to its row), spotlights, extracts, consolidates, applies.

**Decide and state:** does this reuse `memory_compilations.chat_through` as its watermark, or need its own column? They are different sources and sharing one watermark would let a repo compile advance chat's position. Say which you chose and why; if a new column is needed, that is a small migration and is in scope.

- [ ] **Step 4: register the handler and wire it to the worker** so ingestion eventually compiles, following how `pipeline/chat.py` debounces and enqueues.

- [ ] **Step 5: run, including a live compile (`-m live`), and commit.**

---

## Task 5: The agent can read repository activity

**Files:**
- Modify: `agent/tools.py`, `agent/agent.py`, `agent/registry.py`
- Create: `tests/test_repo_tools.py`

- [ ] **Step 1: failing tests**, including the isolation ones this codebase now expects by default:
  - a TEAM_B repo event never appears in a TEAM_A query, for a member of both teams, proven non-vacuously;
  - the tool reads as the requesting member (`user_session`), never under a worker role;
  - output is capped and flags truncation.

- [ ] **Step 2–4: implement** `repo_activity(query|since, limit)` reading `github_activity` joined to `github_repos`, as the requesting member, with an explicit `team_id` filter. Register as `ToolSpec("db", writes=False, needs_human=False)`. Teach the agent in the instruction when to reach for it: to answer "what changed in the repo", not to summarise code.

- [ ] **Step 5: run backend + live, commit.**

---

## Phase 3 exit criteria

- [ ] `uv run pytest` green; `uv run pytest -m live` green
- [ ] Frontend build, unit (`--no-file-parallelism`), integration, e2e green
- [ ] `supabase db reset` from scratch; roles re-applied; suite green
- [ ] **An unsigned webhook delivery is refused, and an empty configured secret refuses everything**
- [ ] **A replayed delivery id enqueues exactly one job**
- [ ] **A bot-authored issue does not reach the compiler**, proven by test
- [ ] Repo text reaching the model is spotlighted
- [ ] A compiled fact carries a `github` citation
- [ ] Every new tool has a `REGISTRY` entry; `test_every_registered_tool_is_declared` passes
- [ ] `graphify` refreshed; branch merged to `master`
