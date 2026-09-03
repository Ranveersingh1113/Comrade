# SDD ledger — plan: docs/superpowers/plans/2026-08-30-comrade-v2-phase-3.md

Branch: feat/comrade-v2-phase-3, cut from master after the Phase 2 merge (c1ad50a).
Baseline: 321 backend, 7 live, 83 frontend, 19 integration, 7 e2e.

Batching:
  Task 1 inline    — a security primitive; standalone commit so it reads without a feature diff
  Task 2 inline    — one migration, mostly grants
  Task 3 SUBAGENT  — the first unauthenticated route + the ingest job
  Task 4 SUBAGENT  — the compile path; where §22.3 provenance bites
  Task 5 inline    — one read tool

Verified before planning, not assumed:
  profiles.github_username EXISTS and is unused — the author_github -> author_user_id path
  github_activity.node_type is check-constrained to commit|pr|merge — needs widening
  jobs.job_type has no 'ingest_github'
  comrade_pipeline has SELECT on github_activity, NOTHING on github_repos — needs insert/select
  authenticated holds FULL CRUD on github_activity by Supabase default — members must not write
    repository history; revoke down to select
  memory_citations.source_kind already allows 'github', AND trg_memory_citation_source_team's
    else-branch already looks up github_activity — citations need no schema work
  contribution_v already counts github_events, so the contribution screen's GitHub half lights up
    for free once ingestion writes rows

Scope note: this phase is READ-ONLY. No tokens held, nothing pushed, and no GitHub App needed —
repo webhooks with a shared secret suffice for ingestion. §16.3's installation-token requirement
governs WRITES, which belong with the sandbox phase.

Task 1: complete (commit 9905239, inline). 334 passed (was 321).
  🔴 MY OWN SECURITY TEST WAS VACUOUS AND MUTATION-TESTING CAUGHT IT.
  test_the_comparison_is_constant_time grepped inspect.getsource() for "compare_digest" and
  stayed GREEN after I swapped the call for `==` — because the string still appeared in the
  DOCSTRING. It now asserts against verify_signature.__code__.co_names, the compiled call rather
  than the prose about it. A security test that cannot fail is worse than no test: it reads as
  coverage. Both mutations now go red (empty-secret guard removed; compare_digest -> ==).
  Fails closed on an empty secret — hmac will happily sign with b"", so an unconfigured
  deployment would otherwise accept whatever a caller signed with it. Same class of bug Phase 1
  fixed in the opposite direction (user_session falling back to a BYPASSRLS connection).

Task 2: complete (commit 307089d, inline). 348 passed.
  The grant that mattered was a REVOKE: members held Supabase's default INSERT/UPDATE/DELETE on
  github_activity. A member INSERTING there would fabricate repository events that the compiler
  turns into CITED WIKI FACTS — putting words in the repo's mouth. Third time this default has
  mattered (agent_steps, the architecture.md rule, now this); it is a tested invariant here.
  Pinned a github citation end to end: the citation trigger's else-branch already handled it, but
  nothing had ever exercised the path because nothing wrote the table.

Task 3: complete (commit 04555e5). Subagent hit the session limit with tests red and the handler
  written but the ROUTE not yet implemented; controller wrote the route and finished inline.
  363 passed (was 348).
  Route decisions: raw body verified BEFORE parsing (parse-then-verify hands the JSON parser
  unsigned attacker input); an unregistered repo is accepted-and-dropped rather than 404'd,
  because GitHub retries non-2xx forever AND a distinguishable response is a registration oracle
  for anyone holding the secret; an unmapped event type is ignored deliberately so the queue does
  not fill with permanent failures.
  Controller mutation checks: bypassing verify_signature -> 3 red INCLUDING the fail-closed test
  pinned at the route (not just in the helper's own tests); dropping the delivery id -> replay
  test red.

Task 4: complete (commit d47f774, subagent). 386 passed (was 363, +23), live 8 (was 7).
  🔴 THE PROVENANCE RULE PROVEN LIVE, not asserted: a real Gemini compile over 3 eligible rows
  produced 5 facts with 5 `github` citations and ZERO facts from the bot issue or the unmerged PR.
  Controller mutation checks:
    remove both bot filters      -> 6 RED (incl. the by-id refetch and the debounce-count tests)
    make unmerged PRs eligible   -> test_an_unmerged_prs_description_never_reaches_the_compiler RED
  Two INDEPENDENT bot signals: the flag recorded at ingest AND the '[bot]' login suffix, the
  latter catching rows ingested before the flag existed. Bot events are still STORED (contribution
  counting is a different question) and filtered only on the compile path.
  Watermark decision: NEW column memory_compilations.github_through, not a reuse of chat_through —
  sharing one would let a repo compile advance chat's debounce past messages nobody compiled,
  silently and unrecoverably. Tracks github_activity.created_at (ours, monotonic) rather than
  GitHub's nullable occurred_at, so a late delivery cannot fall behind the watermark forever.
  Provenance tests assert on the actual `contents` handed to a faked Gemini client, not on return
  values — the rigour that makes them worth having.
  Reasoned deviations: commits/pushes excluded (no review gate; §16.6 names titles/descriptions/
  review comments) — reversible in one list. A `bot` flag added to the stored payload at ingest
  because Task 3's extractors dropped user.type entirely; that is recording provenance, not
  filtering at ingestion.

Task 5: complete (commit b9d6b72, inline). 393 passed (was 386), live 8.
  repo_activity reads the COMPILED EVENT RECORD, never GitHub's API — migration one's own comment
  on the table says "AI queries this, not the raw repo". The tool holds no token and cannot be
  talked into fetching anything; the only path repository data enters is the signed webhook.
  Cross-team test non-vacuous: proves the member CAN read the other team's row first.
  Controller slip worth noting: shell heredoc escaping mangled a "\n" literal into a real newline,
  producing an unterminated string. Two scripted repair attempts also failed to the same escaping.
  Fixed with the Edit tool. Lesson: for source containing escape sequences, edit the file directly
  rather than generating it through a shell heredoc.

PHASE 3 EXIT CRITERIA — all green (2026-08-31):
  uv run pytest              393 passed, 8 deselected   (phase start: 321)
  uv run pytest -m live        8 passed
  npm run build              clean
  npx vitest --no-file-parallelism   83 passed / 11 files
  npm run test:integration    19 passed (1 realtime flake first run, green on retry — pre-existing)
  npx playwright test          7 passed
  supabase db reset          31 migrations from scratch, roles re-applied, 393 + 8 live green after

  Behavioural criteria verified END TO END:
    unsigned delivery refused; empty configured secret refuses even a correctly-signed one,
      pinned AT THE ROUTE not just in the helper
    replayed delivery id enqueues exactly ONE job
    bot-authored content cannot reach the compiler (2 independent signals, mutation-checked)
    live compile: 3 eligible rows -> 5 facts, 5 github citations, ZERO from bot or unmerged PR
    repo text reaching the model is spotlighted
    members can read repository history but never author it
