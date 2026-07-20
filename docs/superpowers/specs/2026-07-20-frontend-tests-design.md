# Frontend Test Suite — Design

Date: 2026-07-20 · Status: approved (owner, in-session)
Scope: `frontend/` (React 19 + Vite + TS + supabase-js). Backend surface as of
commit `4bcbcaa` (JWT ES256 auth, consent tiers/T3, invites, realtime published).

## Goal

A four-layer, *real* test suite — not smoke tests. "Real" per the owner's
standing direction: assumptions get verified against the actual local Supabase
stack and, at the top layer, a actual browser. The auth ES256 incident is the
motivating example: a mocked layer happily passes while the real one is broken.

Coverage bar: 80% (repo standard) on `src/lib`, `src/hooks`,
`src/components`, and the extracted view-models. Screens earn coverage via
component + E2E layers, not line-targeting.

## Non-goals

- No visual-regression/screenshot testing.
- No CI wiring (no CI exists yet); scripts must be CI-ready but local-first.
- No testing of the Python backend here (157 pytest tests own that).

## Layer 1 — Pure logic (Vitest, node environment)

**Targets:** `lib/format.ts`, plus view-model logic extracted from oversized
screens:

| New module | Extracted from | Logic |
|---|---|---|
| `lib/taskFlow.ts` | Tasks.tsx (339 ln) | status transitions; which affordance (CONFIRM/START/MARK DONE/waiting) each viewer sees; assignee gating |
| `lib/roomModel.ts` | GroupRoom.tsx (734 ln) | message grouping, diff-card/observation/deletion classification, contribution bars |
| `lib/wikiModel.ts` | Wiki.tsx (317 ln) | facts→pages projection, Uncategorized bucket, revision ordering, revert-queued state |
| `lib/consentModel.ts` | ConsentCard.tsx (323 ln) | tier labels, awaiting-second-key/awaiting-approval state machine, literal-args rendering prep |

The extraction is behavior-preserving refactoring (screens keep rendering the
same DOM) and is REQUIRED — GroupRoom exceeds the 800-line file rule's warning
zone and its logic is currently untestable.

## Layer 2 — Component (Vitest + @testing-library/react + jsdom + MSW)

Supabase client mocked at module boundary (`lib/supabase.ts` vi.mock); agent
API faked with MSW request handlers. Assertions are product invariants:

- **ConsentCard**: literal tool name, literal args JSON, source snippet all
  visible (hard design rule); approve/reject/edit call the right endpoints;
  **409 renders "went stale, ask again" distinctly from 404**; T3 shows
  two-key state.
- **MemoryDiffCard**: expands to versions; revert click inserts
  `memory_reverts` with the member's id; queued state.
- **Tasks screen**: confirm button only for the assignee; waiting copy for
  everyone else.
- **GroupRoom**: deletion placeholder (never a silent gap); AI badge; empty
  reply doesn't render an empty bubble.
- **Sidebar**: unread dot, pending-consent count; ErrorBoundary contains a
  crashing screen (regression: the double-subscribe blank-app bug).
- **PrivateThread**: nudges render; send box present.

## Layer 3 — RLS integration (Vitest, node, real local Supabase)

Gated like the Python DB tests (skips cleanly when the stack is down).
Global setup: create test users **through GoTrue** (password grant) so tokens
are real ES256; seed teams/memberships/rows via a `pg` dev-dependency on
`COMRADE_DB_URL_ADMIN`. Each file cleans up its team.

Client-side proofs (all via supabase-js as signed-in users):

1. Private thread rows invisible to a teammate; group rows visible.
2. `memory_versions` insert/update rejected for members (read-only wiki).
3. `memory_reverts` insert allowed for own member_id, rejected for others'.
4. consent_queue: T2 invisible to teammates, pending T3 visible; teammate
   update limited to second-key columns (trigger errors surface).
5. `document_opens` per-viewer; `document_opens_summary` returns counts to a
   non-opener; outsider sees nothing.
6. **Realtime round-trip**: subscribe to messages, insert from a second
   client, event arrives (bounded wait). Private-thread inserts do NOT leak
   to a teammate's subscription.

## Layer 4 — E2E (Playwright, thin)

Runs against `vite dev` + `uvicorn` + local stack (Playwright `webServer`
config boots both). Password login (test users from layer-3 setup; magic link
stays out of E2E). Journeys:

1. Login → team gate → group room renders roster + messages.
2. Send a group message; it appears (via Realtime, not refetch).
3. Assignee confirms a task; non-assignee sees waiting copy (two contexts).
4. Wiki: revert a fact; REVERT QUEUED appears; diff card links.
5. Consent: approve a T2 item → executed.
6. **T3 two-key with two browser contexts** — leader approves (awaiting
   second key), teammate countersigns, action executes, room shows the post.
7. Observation suppress: card gains tombstone, suppression recorded.
8. (Gated on `GEMINI_API_KEY`) private-thread agent turn round-trip.

## Tooling & scripts

Dev-deps: `vitest`, `@testing-library/react`, `@testing-library/user-event`,
`@testing-library/jest-dom`, `jsdom`, `msw`, `@vitest/coverage-v8`,
`playwright`/`@playwright/test`, `pg` (+`@types/pg`).

```
npm test               # layers 1+2, no stack needed
npm run test:integration  # layer 3, requires local stack
npm run test:e2e       # layer 4, requires stack + uvicorn (+ key for #8)
npm run test:coverage  # 1+2 with v8 coverage, 80% threshold
```

## Risks / notes

- React 19 + RTL: needs current @testing-library/react (v16+); jsdom quirks
  with Vite env vars → `import.meta.env` stubbed in `vitest.setup`.
- Realtime tests are timing-sensitive: bounded waits (5s) + retry once, skip
  with a loud message if the realtime container is down.
- E2E test users must not collide with Python-suite seeds: dedicated
  `fe-*@test.dev` emails, cleaned in teardown.
- The GroupRoom/others extraction must land BEFORE component tests are
  written against the old structure.
