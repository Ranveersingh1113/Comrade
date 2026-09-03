# SDD ledger — plan: docs/superpowers/plans/2026-08-29-comrade-v2-phase-0.md

Branch: feat/comrade-v2-phase-0 (cut from feat/frontend). No git worktree: the
local Supabase DB is shared state, so a worktree would not isolate the part
that matters (migrations) while adding path confusion. Branch isolation is the
boundary here.

Batching (controller judgement, per user request 2026-08-29):
  Batch A = Tasks 2+3+4   — three tiny independent fixes, no shared files
  Batch B = Tasks 5+6     — T3 removal, backend then frontend (one logical change)
  Task 7                  — group-post removal (has the 13.5 "don't trust green" trap)
  Task 8                  — reads-as-requester (the real refactor)
  Batch C = Tasks 9+10    — memory render + page descriptions (shared files)
  Task 11                 — connection pool
  Task 12                 — controller-inline (prose judgement + user memory dir)

Pre-flight scan of tasks 2-12 (findings were plan-author defects, fixed by the
controller before dispatch, not human adjudications):
  - Task 3's second test asserted a mock was reached rather than behaviour.
    Replaced with `assert resp.status_code != 422`.
  - Task 12's commit staged `docs/`, which is now gitignored. Narrowed to the
    tracked files.
  - Task 11 risk carried into its dispatch: `pipeline/worker.py` sets
    `conn.autocommit = True` on a `connect(Role.ADMIN)` connection; pooled
    connections may reject that or fight the pool's commit-on-exit.
    tests/test_worker.py is the net.

Task 1: complete (commits 710ea7c, review clean — executed pre-skill, verified
        against the live DB; full suite 182 passed)

Batch A (Tasks 2,3,4): complete (commits 03f26e1..108c52d, review clean — spec ✅ x3, quality approved)
  Task 2: minor (deferred): brief's grep-gate hit count was a miscount (doc/vendor noise); live path untouched
  Task 3: minor (deferred): TEAM/USER in tests/test_server.py changed placeholder->UUID; verified inert at all 14
          call sites today, but removes a loud-crash signal if a future test in that DB-stubbed file forgets a stub
  Task 3: plan defect found by implementer — brief's Step 5 `git add` named tests/test_consent_tiers.py while
          Step 1 and the Files section said tests/test_server.py. Implementer committed what actually changed.
  Task 4: CREATE INDEX CONCURRENTLY applied cleanly via `supabase migration up`; fallback never needed

Batch B (Tasks 5,6): fix round 1/5 (3 addressed, 0 open — stale T3 comment in consent.py; false
        audit-trigger claim in ConsentCard.tsx; lost raise-above-floor coverage; commit cb2f55b)
Batch B (Tasks 5,6): complete (commits 108c52d..cb2f55b, review clean — spec COMPLIANT, quality approved)
  Controller ruling: findings 1+2 were plan-mandated TEXT that the same task made factually false
  (my brief authored both comments). Ruled plan defects, not plan conflicts — same class as the wrong
  `git add` path and the omitted second_approved_at. Fixed via the implementer, not escalated.
  Verified independently before dispatching: live DB has ZERO triggers on consent_queue, and
  `reversible` is read only by the ConsentCard badge.
  Implementer caught that the controller's own suggested replacement text was ALSO false
  (team_propose_group_message is still registered until Task 7) and wrote accurate text instead.
  Task 5: minor (deferred): `assert` in resolve_tier is stripped under python -O
  Task 5: minor (deferred): test_teammates_still_cannot_see_lower_tier_items deleted; equivalent RLS
          coverage now survives only in frontend/tests/integration/rls-consent.test.ts:34-40
  Task 6: minor (deferred): ConsentInbox.tsx:65 T2 legend cell keeps a trailing borderRight
  Task 6: minor (CARRIED INTO TASK 7): consentModel.test.ts:6 and e2e/global-setup.ts:73-75 still name
          post_group_message — Task 7's grep gate must catch these
  Task 6: minor (deferred): e2e/journeys.spec.ts numbering gap at 6 (disclosed; memberPage still used)

Task 7: complete (commit 9dac8e3, review clean — reviewed INLINE by the controller, not a subagent:
        user directed 2026-08-29 that per-task review subagents waste tokens re-deriving context.
        Verified: grep gate = 4 hits, all acceptable (2 historical migration comments, 2 in the
        deliberate pin-test asserting absence); _EXECUTORS/_PRECHECKS = task_create only, no orphaned
        helpers; agent tool list = exactly 4; instruction prose coherent with no dangling clause;
        ConsentCard reversible comment now factually true (3rd rewrite, verified against agent/tools.py);
        migration revokes INSERT + drops ex_messages_insert without over-revoking; live DB shows
        comrade_executor = SELECT only on messages, INSERT/SELECT/UPDATE intact on tasks;
        full suite 173 passed, kept-writes suite 8 passed)
  Task 7: implementer found 2 references beyond the brief AND beyond the 2 carried forward
          (tests/test_consent_tiers.py, evaluation/scenarios.py) — the §13.5 trap confirmed live:
          grep caught them, pytest never would have
  Task 7: minor (deferred): one transient realtime.test.ts integration flake, pre-existing infra

Task 8: complete (commit 1aeae0e, review clean — reviewed INLINE by the controller)
  Verified: comrade_agent read surface collapsed 21 tables -> 5, exactly the survivors list
    (messages INSERT-only, consent_queue INSERT-only, nudge_log +SELECT, agent_runs, change_log).
    NO SELECT on messages -> the §2.1 private-thread leak is structurally gone, not policed.
  Implementer found a real gap in my brief: INSERT..RETURNING requires SELECT on the table, so the
    brief's own revokes broke _persist_ai_reply and propose_action. Its fix (generate the uuid in
    Python, drop RETURNING) is BETTER than granting a narrow SELECT back — it leaves zero read
    surface. Verified no non-internal triggers on consent_queue can rewrite the literal 'pending'.
  MUTATION CHECK by controller: removed the team_id filter from the tasks query -> 3 tests went red
    including test_team_state_excludes_the_requesters_other_team. Restored, 6 passed. The leak tests
    have teeth and are non-vacuous (the sharp one proves A1 really can see TEAM_B before asserting).
  Live Gemini test ran and passed: the agent still reads the wiki, now as the member.
  Full suite 180 passed (baseline 173, +7). All callers of the 3 changed signatures updated.
  Migration idempotent: every drop guarded `if exists`.
  Task 8: minor (deferred): `supabase db reset` not run (would need re-applying local role passwords);
          forward migration applied cleanly and is deterministic by inspection
  Task 8: two tests in test_rls_isolation.py updated from "agent CAN read, scoped" to
          "agent gets InsufficientPrivilege" — same invariant, now enforced at the grant layer

Batch C (Tasks 9,10): complete (commits 40c7183, 0e9c430 — review clean, reviewed INLINE)
  Verified: annotate() factored BETTER than my brief — returns bare text, callers prepend their own
    prefix, instead of the brief's `annotate(f)[2:]` slice hack. Consolidation prompt still shows
    `- [entry_id] fact` with the id first, which is what the model matches decisions on.
  Controller ran an end-to-end render against the live DB:
    "- Demo is Friday  _(as of 2026-08-29, from a doc)_" and, for a fact with no citation,
    "- deadline is Friday  _(as of 2026-08-29)_" — degrades gracefully, no empty parens.
  Live Gemini compile ran: 3 passed, and all 4 pages it created got non-empty descriptions
    (e.g. "Shipping Cadence" / "Information on when the team ships deliverables.").
  _resolve_page fills a blank description, never overwrites — first compiler to name a page wins.
  Full suite 184 passed (baseline 180, +4).
  Incidental confirmation: the memory_citations team-match TRIGGER fired on the controller's own
    scratch probe (bad source_id) — the DB-enforced citation invariant is live and working.

Task 11: complete (commit f54e0d1 — implemented AND reviewed INLINE by the controller, no subagent)
  Measured win: full suite 32s -> 19s. 193 passed (baseline 184, +9 pool tests).
  🔴 The important finding: my first version of the cross-tenant guard was WORTHLESS.
    "Alternate TEAM_A/TEAM_B across borrows and assert the scope follows" stayed GREEN under a
    mutation that made app.current_team_id session-scoped — because every scoped borrow overwrites
    the setting with the right value, so the leak is invisible from inside a scoped borrow.
    The leak is only observable from a borrow that sets NO scope of its own. connect(Role.AGENT)
    shares a pool with team_session(Role.AGENT, ...), so a BARE borrow after a scoped one exposes it.
    Both guards are now mutation-verified:
      set_config('app.current_team_id', %s, false)  -> bare-borrow team test FAILS
      "set role" instead of "set local role"        -> bare-borrow identity test FAILS
  Also corrected: the first draft asserted a specific pg_backend_pid across borrows. A pool gives no
    such guarantee (async return; the pool had grown to 2), so that was a scheduling coincidence.
    Tests now assert N borrows != N backends.
  autocommit collision investigated by running it, not by reasoning: pipeline/worker.py sets
    conn.autocommit=True on a connect(Role.ADMIN) borrow. Resolved in shared/db.py with a pool
    `reset` hook clearing autocommit on return — no worker call sites edited.
  CONTROLLER MISTAKE worth recording: used `git checkout -- shared/db.py` to undo a mutation on a
    file with UNCOMMITTED work, and wiped the implementation. Redid it. For mutation checks on
    uncommitted code, back the file up to a temp path and restore from that.

Task 12: complete (commit 46283bb — stale records corrected, controller-inline as planned)
  README: engineering teams primary (§14) + the "coherence at speed" positioning (§22);
    the false "never performs a group-visible write" invariant fixed in README AND architecture.md.
  HANDOFF §7: T3 recorded as removed, WITH the uncovered consequence (§24.1) — after the removal
    non-code actions have no forced second pair of eyes and nothing plans one.
  architecture.md: consent state diagram loses awaiting_second_key; role table shows AGENT with no
    read of team data and EXECUTOR writing tasks only; documents the read-scope change and the pool.
  findings doc: appended §26 (number checked for collision first, per §24.5-2) recording the two
    findings that correct the document itself (26.1 the RLS group-reply bug, 26.2 the worthless
    pool guard) plus 26.4 — §24.6's own stale-records list was itself stale about README:3.
  project memory updated: target = developer/engineering teams primary.

PHASE 0 EXIT CRITERIA — all green (2026-08-30):
  uv run pytest              193 passed, 5 deselected
  uv run pytest -m live        5 passed (real Gemini)
  npm run build              clean
  npm test                    75 passed / 11 files  (SERIALLY — see note)
  npm run test:integration    19 passed
  npm run test:e2e             7 passed in 29.2s, including the live agent turn
  grep gate                  3 hits, all deliberate (1 comment documenting a deletion,
                             2 in the pin-test asserting post_group_message has no executor)

  NOTE on flakiness, diagnosed not assumed: vitest worker forks crash and Playwright hooks time out
  when Docker's 11 Supabase containers + uvicorn + parallel pytest all compete on this machine
  (7.4GB). Evidence it is contention and not code: a single e2e test took 1.2 MINUTES during
  contention vs all seven in 29 SECONDS once settled; a different test failed on each run; the
  Playwright errors were hook timeouts and an internal "guid not bound", never assertions; and
  `vitest --no-file-parallelism` is fully green. Workaround, not a fix: run frontend suites serially.

FINAL WHOLE-BRANCH REVIEW (Opus, one dispatch): CHANGES REQUIRED -> fixed -> clean.
Full review at final-review.md. It found what per-task reviews structurally could not.

  🔴 CRITICAL, FIXED (commit 808113b): a SECOND private-thread leak, in `agent_runs`.
     input_summary holds the member's prompt verbatim, steps holds every tool result, and
     au_agent_runs_select was is_team_member(team_id) — so ANY teammate could read ANY member's
     private turn from the browser. WORSE than §2.1, which was latent; this was live.
     Controller reproduced it before fixing (A2 read A1's prompt verbatim) and re-verified after
     (A2 -> InsufficientPrivilege). Fixed by DELETING the grant, not narrowing it: nothing
     member-facing reads the table. _check_turn_budget moved to the AGENT role — team-scoped by
     current_team(), returns a COUNT to the server, never rows to a member.
     Missed by: the findings doc, the v2 plan, Task 8's brief, and every per-task review.

  🟠 IMPORTANT, FIXED (808113b): comrade_executor still held SELECT on messages — team-scoped,
     thread-scoped by nothing, the §2.1 shape on a sibling role. My own Task 7 migration revoked
     that role's INSERT for exactly this reasoning and left the SELECT.
  🟠 IMPORTANT, FIXED (808113b): README said "five function tools"; four since §13. A NEW falsehood
     introduced by the commit whose only job was correcting stale text.

  🟠 IMPORTANT, PARKED — ruling: real, bounded, and Phase 1 work, not a Phase 0 blocker.
     au_consent_queue_update restricts ROWS and nothing else, so a requester can UPDATE arbitrary
     columns on their OWN pending row — rewind status to 'pending' and re-approve to execute twice,
     or rewrite team_id/action_hash/tier. Exactly-once holds against retries, races and the agent,
     but not against the requester. Pre-existing, not introduced here. The fix is a column-restricting
     trigger — the same mechanism as the T3 guard just deleted — which belongs with Phase 1's tool
     registry and chokepoint (F4). CARRIED into the v2 plan so it cannot be lost.

  PROMOTED MINOR, DONE: `supabase db reset` run. All 6 migrations apply from scratch, including the
     repo's first CREATE INDEX CONCURRENTLY and the NOT VALID -> VALIDATE sequence. Local role
     passwords re-applied from scripts/setup_local_roles.sql. Suite green on the rebuilt DB.

  MINORS CARRIED to Phase 1 (see the v2 plan): consent resolution lacks a team_id clause;
     user_session falls back to the BYPASSRLS admin URL when the authenticator URL is unset and now
     shares its pool; user_session runs with statement_timeout=0 because role GUCs do not apply on
     SET ROLE; a duplicate proposal now 500s the whole turn via the new unique index.

  Reviewer verified clean and I did not re-verify: every alternative read path probed and denied
     (both views incl. document_opens_summary with security_invoker=off, all reachable SECURITY
     DEFINER functions, change_log, the auth schema, role escalation, DDL, default ACL); all 16
     user_session queries AST-scanned for team scoping; pooled-connection carryover measured across
     role, search_path, jwt claims and both app GUCs.

PHASE 0 COMPLETE — final gates on a database rebuilt from scratch:
  uv run pytest 196 · -m live 5 · npm test 75/11 files (serial) · integration 19 · e2e 7 (34.7s)
