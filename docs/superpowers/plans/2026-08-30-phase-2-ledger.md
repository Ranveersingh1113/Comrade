# SDD ledger — plan: docs/superpowers/plans/2026-08-30-comrade-v2-phase-2.md

Branch: feat/comrade-v2-phase-2, cut from master after the Phase 1 merge (d28e701).
Baseline: 262 backend, 6 live, 76 frontend, 19 integration, 7 e2e.

Batching (controller judgement):
  Task 1 inline    — two migrations + one compiler line
  Task 2 inline    — one index + a query rewrite; the point is an EXPLAIN plan, not a diff
  Task 3 SUBAGENT  — messages_search + document_read, the phase's centrepiece
  Task 4 SUBAGENT  — three tools + a new consent executor
  Task 5 inline    — a view change + one read tool
  Task 6 SUBAGENT  — backend + frontend batching UI

THE UNLOCK: findings §2.1 warned the private-thread leak goes LIVE the moment a message-reading
tool exists. That tool is Task 3. Phase 0 closed it by deleting the agent's read grants, so
messages_search is safe to build BECAUSE reads now run as the requesting member and
au_messages_select restricts private rows to their owner. Do not add a Role.AGENT read path back
to make any of these tools easier.

Design constraint found while planning Task 4 (not in the findings doc): trg_tasks_confirm_guard
enforces "only the assignee may confirm their own task" by checking auth.uid(). The executor runs
as comrade_executor where auth.uid() IS NULL, so ANY executor-driven move out of 'proposed' or set
of confirmed_at raises. That is correct — an AI-proposed update must never confirm a task on a
human's behalf — so task_propose_update may amend title/description/deadline/assignee only, never
status. Reassignment is fine; the trigger correctly voids a prior confirmation.

Task 1: complete (commit 36b5303, inline). 266 passed (was 262).
  documents.parsed_text stored UNMARKED — spotlight() is a presentation-time defence applied on
  the way into an LLM call, not a storage format. document_read must re-spotlight.
  idx_messages_fts: GIN over a to_tsvector EXPRESSION, so messages_search must repeat the
  expression verbatim or the planner ignores the index.

Task 2: complete (commit 0f069b1, inline). 267 passed.
  MEASURED, not assumed. Same 6k-message corpus:
    before (correlated subquery)  Seq Scan, SubPlan x6001, 12,101 buffers, 22.852 ms
    CTE form (my first attempt)   subplan gone, STILL Seq Scan, index UNUSED, 6.066 ms
    lateral from `teams`          Bitmap Index Scan on idx_messages_group_human, 0.097 ms
  The CTE form is the instructive failure: hoisting the watermark removed 6001 subplan
  executions and looked like a win, but a predicate that depends on a JOINED ROW cannot become
  an index condition, so it still scanned. An unused index on `messages` — the hottest insert
  path in the schema — is pure write-side cost. Driving from `teams` gives the inner count a
  literal t.id, which is what lets the partial index work.
  Lesson: "the subplan is gone" is not "the query is fixed". Check the scan node, not just the
  absence of the thing you removed.

Task 3 (messages_search + document_read) dispatched to a subagent — the phase centrepiece.

Task 3: complete (commit a98fced). Subagent hit the session rate limit while writing its report,
  with the implementation finished but uncommitted; controller verified and committed inline.
  282 passed (was 267), live 7 (was 6).
  🔴 THE §2.1 PAYOFF, PROVEN not asserted: A1 and A2 each hold a private message matching
  "salary review" in the same team. A1's search returns ONLY their own. The leak the findings doc
  said would "go live the moment a message-reading tool exists" is closed by Phase 0's grant
  deletion, and this is the evidence.
  Controller mutation checks (subagent was cut off before its own):
    neuter the team_id filter   -> test_a_team_b_message_never_appears_in_a_team_a_search RED
    neuter the tombstone filter -> test_a_tombstoned_message_is_not_returned RED
  Index confirmed in use: Bitmap Index Scan on idx_messages_fts. The query repeats
  to_tsvector('english', body) verbatim — a paraphrase silently drops to a seq scan.
  document_read SPOTLIGHTS on the way out: parsed_text is stored unmarked (it is source text, not
  a prompt) but is attacker-controlled, so the datamarking the compile path applies is applied
  here too. Payloads capped with a `truncated` flag so the model knows it sees part of something.

Task 4: complete (commit 5b388c9, subagent). 305 passed (was 282), live 7.
  The trg_tasks_confirm_guard constraint held: the executor runs as comrade_executor where
  auth.uid() IS NULL, so it cannot confirm a task on a human's behalf. task_propose_update is
  restricted to title/description/deadline/assignee_id.
  Subagent went FURTHER than the brief and was right to: a proposal containing any forbidden key
  is refused ENTIRELY, before any SQL, so a human never sees a card that partially lied about
  what happened. Controller verified end to end:
    status change  -> "task_update may only set (title, description, deadline, assignee_id),
                       got ['status']"; consent row stays 'approved', never 'executed'
    deadline change -> executed; title, assignee and status all untouched
  Subagent ran its own mutation checks on the key guard, partial-update logic and cross-team filter.

Task 5: complete (commit 04bc4aa, inline). 312 passed (was 305).
  Two bugs I wrote and caught before commit, both worth recording:
    1. coalesce(..., '-infinity') as a greatest() sentinel works in SQL and then fails on the way
       out — psycopg raises DataError: timestamp too small (before year 1). Postgres greatest()
       already ignores nulls and returns null only when ALL args are null, which is the wanted
       semantics with no sentinel.
    2. days_since_last_signal was computed in Python against a Postgres timestamp — two clocks.
       It passed locally and is exactly the kind of thing that surprises you at 3am on a host
       whose clock drifted. Moved into SQL: extract(day from now() - greatest(...)).
  last_message_at counts GROUP messages only: a private thread with the AI is not evidence of
  team participation, and counting it would mean a member who talks only to the bot never looks
  idle — the exact case the nudge exists for.
  Governance ruling 6 honoured: recency facts in NAME order, never a ranking, and the docstring
  says so where the next person will read it.

Task 6 (propose_batch) dispatched — backend + frontend.

Task 6: complete (commit 5d79dac, subagent). 321 passed, frontend 83, live 7.
  Controller verified the invariant directly: propose 3 -> approve #1 (executed), reject #2,
  #3 still 'pending' and then executes. 2 tasks created, not 3. A batch is a display grouping,
  never an all-or-nothing gate — bundling something contentious with four obvious things to
  extract approval for the lot is the consent-fatigue failure the findings doc warns about.
  Subagent chose best-effort over atomic partial failure and said so rather than defaulting.
  Grouping logic went into frontend/src/lib/ (tested) per the codebase's existing convention.
  Honest gap it flagged rather than hid: no authenticated browser screenshot of the grouped UI
  (seeded users have no usable UI password); 13 unit tests + a dev-server boot check instead.

LIVE-TEST FLAKE, diagnosed rather than papered over: test_the_second_turn_remembers_the_first
  failed ~1 full-suite run in 5 while passing 3/3 in isolation — sampling noise from asserting on
  LLM output, not interference. Now takes two samples: a genuinely broken history feature fails
  BOTH (the codename exists nowhere else in team state or wiki), so it is still a real gate.
  A suite that goes red on noise is worse than useless — it trains people to ignore red. 3/3 after.

PHASE 2 EXIT CRITERIA — all green (2026-08-30):
  uv run pytest              321 passed, 7 deselected   (phase start: 262)
  uv run pytest -m live        7 passed  (3 consecutive full runs)
  npm run build              clean
  npx vitest --no-file-parallelism   83 passed / 11 files
  npm run test:integration    19 passed  (1 realtime flake first run, green on retry — pre-existing)
  npx playwright test          7 passed
  supabase db reset          29 migrations from scratch, roles re-applied, 321 green after
  graphify refreshed         1201 nodes, 2350 edges

  Behavioural criteria verified END TO END:
    A1 and A2 each hold a private message matching "salary review"; A1's search returns only
      their own — the §2.1 leak is closed and this is the proof
    Bitmap Index Scan on idx_messages_fts (the tsvector expression matches the index verbatim)
    chat sweep: 22.852 ms -> 0.097 ms on the same 6k corpus, index in use
    task_propose_update refuses a status change entirely, consent row never reaches 'executed'
    batch items approve and reject independently
