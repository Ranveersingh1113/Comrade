# SDD ledger — plan: docs/superpowers/plans/2026-08-30-comrade-v2-phase-1.md

Branch: feat/comrade-v2-phase-1, cut from master after the Phase 0 merge (fd978df).
No worktree: the local Supabase DB is shared state, so branch isolation is the boundary.

Batching (controller judgement, per the user's 2026-08-29 direction on subagent economy):
  Task 1 controller-inline  — one migration + a 3-line fail-closed fix
  Task 2 SUBAGENT           — new registry + plugin + rewiring runtime/agent/consent (the keystone)
  Task 3 controller-inline  — one trigger migration + tests
  Task 4 SUBAGENT           — threads a reason through 5 files incl. frontend
  Task 5 SUBAGENT           — new table + rewrite of agent_runs.py
  Task 6 SUBAGENT           — new history module + runtime rewiring; the phase's biggest usability win
  Task 7 controller-inline  — advisory lock in one file + tests

PRE-FLIGHT, verified against the installed google-adk 2.2.0 (not assumed from the findings doc):
  RunConfig.max_llm_calls        PRESENT — turn cap is free
  BasePlugin, 12 hooks           PRESENT — before_tool_callback backs the chokepoint
  App(plugins, compaction, ...)  PRESENT — Runner(app=...) accepts it
  DatabaseSessionService         IMPORTS BUT UNUSABLE: ModuleNotFoundError: sqlalchemy

  🔴 PLAN REVISION (F11): the parent plan said "ADK session storage". Two reasons not to:
     (1) it needs SQLAlchemy, a second data-access stack beside raw psycopg3;
     (2) findings §3.1 already warned it creates sessions/events/app_states/user_states
         UNQUALIFIED — they land in `public` beside the RLS tables with no RLS and no team_id.
     Phase 0 spent its whole budget proving that a table holding member content under a coarse
     policy is exactly how private threads leak (twice). Adding four such tables immediately
     after would undo the phase.
     F11 split: F11a RunConfig turn cap (free), F11b conversation history replayed from
     `messages` — already the documented source of truth, already correctly RLS-scoped since
     Phase 0's F1, ~20 lines, no dependency, no new schema.
     Given up knowingly: ADK compaction, resumability, prompt caching. Trigger to revisit is a
     MEASURED context-length failure. App(...) is still adopted in Task 2 so the door stays open.

Task 1: complete (commit 2f1df45, controller-inline). 204 passed (was 196, +8).
  Constraint names confirmed against pg_constraint before writing, not assumed.
  Dropped the two indexes the plan had: nothing queries parent_run_id or batch_id yet, and an
  index on an existing table costs a CONCURRENTLY migration of its own. Buy it when a query exists.
  user_session now raises instead of falling back to the BYPASSRLS admin url.

Task 2: complete (commit 1923844). Subagent hit the session rate limit mid-task with everything
  written but NOTHING committed; controller finished the gates inline rather than re-dispatching.
  214 passed (was 204). Live: 5 real Gemini turns through the new App + plugin.
  The subagent verified short-circuit semantics against ADK 2.2.0 SOURCE rather than assuming:
    PluginManager.run_before_tool_callback runs first, and flows/llm_flows/functions.py calls the
    tool only `if function_response is None` — so a returned dict short-circuits and becomes the
    function response. A refusal reaches the model as a tool result, not a turn-killing exception.
  Controller MUTATION CHECK: flipped registry.UNKNOWN to permissive -> 2 tests red
    (test_an_unknown_tool_fails_closed, test_an_undeclared_tool_is_refused...). Restored, 9 green.
    The fail-closed default is the whole feature and it is genuinely guarded.
  DatabaseSessionService still NOT adopted; session service stays in-memory per turn pending Task 6.

Task 3 (consent column guard) starting: closes the Important finding Phase 0 parked —
  au_consent_queue_update restricts ROWS, not columns.

Task 3: complete (commit below, controller-inline). 224 passed (was 214), 5 live.
  Reproduced all 5 attacker paths before fixing; the double-execution one is the real prize —
  execute_consent's CAS only refuses a row that is not approved/edited, so rewinding status to
  'pending' made it approvable again. Exactly-once held against retries, races and the agent but
  never against the requester.
  Reused the mechanism deleted with T3 (a security definer trigger narrowing human writes,
  worker roles passing through) — the pattern survived its feature.

Task 4: complete (commit cb11e6d, subagent). 234 passed (was 224), frontend 76, live 5.
  Verified inline: explicit team_id filter present, reads via user_session(requester_id) like
  wiki_section, voice is factual not scolding, window 7d (matches propose_action's TTL), cap 5.
  Subagent's cross-team test confirmed non-vacuous by its own mutation check (stripped the team_id
  filter -> TEAM_B's reason leaked into TEAM_A's section).
  NOTE: the subagent hit the SAME `git checkout --` footgun the controller hit in Phase 0 — reverted
  a whole file with uncommitted work while undoing a mutation. Twice now; written to memory as
  feedback_mutation_test_backup.
  Known gap, accepted: no live test stages an actual prior rejection, so "Gemini reasons well about
  a rejection" is unproven; only "the enlarged instruction doesn't break real turns" is.

Task 5: complete (commit 55a3e8b, subagent). 238 passed (was 234), live 5.
  Verified live: agent_steps has comrade_agent INSERT,SELECT only; authenticated REVOKED; RLS on;
  one ag_agent_steps policy. Members get InsufficientPrivilege — the agent_runs leak is NOT
  reproduced in the new table holding the same content.
  🔴 Subagent's key catch, worth more than the task: Supabase grants anon AND authenticated full
  CRUD on EVERY new table by default. Without an explicit revoke the privacy test would have
  returned an empty result instead of InsufficientPrivilege — i.e. the leak would have been
  re-opened and the test would have looked fine. Written into docs/architecture.md as a rule for
  every future migration.
  Controller follow-up: anon still holds default grants on ALL 25 public tables. RLS is on
  everywhere and ZERO policies name anon, so it is default-denied — pre-existing convention, not a
  Task 5 regression. Dead privilege worth a hygiene pass; logged, not expanded into this task.
  Deviations, both correct: no separate index on (run_id, seq) since the unique constraint already
  is that index; added the explicit revoke the brief did not list.
  Subagent also lost ~6 min to `supabase db reset` wiping local role passwords — same footgun the
  controller hit; scripts/setup_local_roles.sql must be re-run after any reset.

Task 6: complete (commit 12f0323, subagent). 255 passed (was 238, +17), live 6 (was 5).
  THE FEATURE PROVED LIVE, with a control:
    turn 1 "we are calling this release Falcon Ridge" -> "Understood."
    turn 2 "What name did I just give the release?"   -> "Falcon Ridge"
    same pair with agent_history_turns=0             -> "The wiki does not record a name..."
  The control is what makes it evidence: it proves history is doing the work, not the wiki.
  Subagent addition beyond the brief, and a good one: group messages are prefixed with the
  sender's display name. Without it every human in a room collapses into one undifferentiated
  role="user" voice and the agent mis-attributes who said what. Verified live.
  CONTROLLER MUTATION METHODOLOGY NOTE: neutering thread_type alone -> 15 green. Neutering
  thread_owner_id alone -> 15 green. Both together -> 4 RED including all three isolation tests.
  The two filters are individually REDUNDANT (group rows have thread_owner_id NULL, so either
  filter alone separates the threads), so a single-filter mutation wrongly suggests vacuous tests.
  Lesson: mutate redundant guards TOGETHER, or you will conclude a good test is worthless.
  Also confirmed au_messages_select already restricts private rows to their owner, so RLS — not
  the app filter — is what stops another member's thread appearing. App filters are scoping.
  ADK DatabaseSessionService deliberately NOT adopted; history replays from `messages`.

Task 7 (room advisory lock) starting — controller-inline.

Task 7: complete (commit 9cf9263, controller-inline). 262 passed, 6 live.
  room_lock: pg_try_advisory_lock on a 64-bit hash of team_id, session-scoped (NOT
  transaction-scoped) because a turn spans several LLM calls and a held transaction would break
  the no-LLM-in-transaction rule. Private threads take no lock at all (§4.3).
  Q6 honoured: a refused turn gets an honest `busy` frame / 409, not a spinner or an empty 200.
  FLAKY-TEST FINDING while verifying: test_the_second_turn_remembers_the_first asserted turn 1's
  reply was non-empty. Measured across identical runs it returns "Understood." / "Okay, Falcon
  Ridge." / "" — the voice guide says be concise and the prompt says "nothing to do about it", so
  SILENCE IS CORRECT BEHAVIOUR. I first suspected my own room-lock change; traced it to the
  prompt by running the exact text three times. Turn 1's reply was never what the test is about.
  Now asserts only on turn 2; stable 4/4.
  Worth keeping: "my change broke it" was the wrong first hypothesis, and a 3x repeat of the
  isolated call was what settled it in ~2 minutes.

PHASE 1 EXIT CRITERIA — all green (2026-08-30), on a DB rebuilt from scratch:
  uv run pytest              262 passed, 6 deselected   (Phase 1 start: 196)
  uv run pytest -m live        6 passed
  npm run build              clean
  npx vitest run --no-file-parallelism   76 passed / 11 files
  npm run test:integration    19 passed  (1 realtime flake on first run, green on retry — the
                              same pre-existing infra flake diagnosed in Phase 0)
  npx playwright test          7 passed in 26.6s
  supabase db reset          all migrations apply from scratch; roles re-applied; suite green
  graphify refreshed         1058 nodes, 1977 edges, 82 communities

  Behavioural criteria verified END TO END, not just by unit test:
    spec_for('some_tool_nobody_wrote_down') -> outbound/writes/needs_human
      and the gate returns refused_by_chokepoint
    a rejection with reason "we already decided this in standup" appears in the next turn's
      instruction under "## Recently rejected"
    live: turn 2 answers "Falcon Ridge" from turn 1's context; with history off it cannot
