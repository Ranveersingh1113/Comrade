# Comrade — Track D: The Product Layer

> Parent plan: [`2026-08-29-comrade-v2.md`](2026-08-29-comrade-v2.md) Part V. Approved 2026-08-29.
> Phase ledgers: [0](2026-08-29-phase-0-ledger.md) · [1](2026-08-30-phase-1-ledger.md) · [2](2026-08-30-phase-2-ledger.md) · [3](2026-08-31-phase-3-ledger.md)

**Goal:** Make Comrade adoptable. Phases 0–3 made it capable — two private-thread leaks closed, a mandatory permission gate, conversation memory, chat/document/repo perception. None of that made it easier to walk into.

---

## 🔴 Re-prioritised on evidence, 2026-08-31

**Part V's original ordering was written without looking at the running app. I have now looked**, signed in against the local stack as a seeded user, and walked every screen at desktop and mobile widths. Two of its six assumptions were wrong, and the order changes as a result.

### What I found

| Item | Part V assumed | What the running app actually shows |
|---|---|---|
| **D5 Responsive** | *"Not addressed anywhere; 1.5 wk; do it last"* | **Completely broken, and the dominant gap.** `grep -rn "@media" frontend/src/` returns **nothing** — there is not one breakpoint in the product. The sidebar is a hard `width: 250`, the group room's rails are `width: 296` and `width: 364`. At 375px the sidebar alone eats two-thirds of the viewport, the room title wraps to three lines, the right rail is cut off mid-word, and the composer is a sliver. The viewport meta tag is present, so this is not a scaling artefact — it genuinely lays out and breaks. |
| **D2 Empty and error states** | *"per-screen empty and failure states largely do not exist; 1 wk"* | **Mostly already done, and done well.** Documents: *"Nothing shared yet — upload a brief, proposal, or WhatsApp export."* Consent inbox: *"Queue clear — nothing awaiting your key."* Tasks, TeamGate and the room's right rail all carry written empty states. The hooks already track an `error` field. The real gap is narrower than stated: not every screen is covered, and nothing distinguishes *"no data"* from *"the request failed"*. |
| D1 First-run | 1 wk | Real, but smaller than assumed — the screens already tell you what to do next. What is missing is the guided path and the bootstrap, not the signage. |
| D3 Consent notifications | 1.5 wk | Confirmed absent. Proposals sit for a 7-day TTL and nothing tells anyone. |
| D4 Team lifecycle | 1.5 wk | Confirmed absent — no leave, no remove-member, no delete, no export anywhere in the tree. |
| D6 Latency perception | 0.5 wk | Partially built: `GroupRoom.tsx` already consumes `streamTurn`'s frames. Needs verification and finishing, not building. |

### The order changes

**Was:** D2 → D1 → D3 → D6 → D4 → D5
**Now:** **D5 → D3 → D4 → D2 → D1 → D6**

D5 moves from last to first. Part V's reasoning for putting it last — *"it is a re-layout of whatever the others produce"* — assumed the other items would substantially reshape the screens. Having looked, they will not: the screens are already well designed, and D2's gap turned out to be small. So the re-layout argument is weak, while *"the product cannot be opened on the device people actually carry"* is decisive.

D3 also pulls D5 forward on its own: a consent request that arrives by email is opened on a phone. Shipping notifications onto a layout that breaks at 375px would be worse than shipping neither.

D2 and D1 drop because the evidence says they are smaller than budgeted. D6 stays last because it is partly done.

---

## Global Constraints

1. **RLS is the authorization layer.** Track D is mostly frontend, but D4 adds real actions — leaving a team, removing a member — and those are DB policy decisions, not UI ones.
2. **Flat authority (§23.1, the product's thesis).** Nobody approves another member's actions and nobody configures permissions. D4's remove-member is therefore *not* an admin power: it must go through the consent protocol like anything else affecting another person.
3. **The design language is already established** — warm paper content, dark sidebar, serif headings, letterspaced small-caps labels, `--ink` / `--paper` / `--lavender` / `--peach-pale` tokens in `frontend/src/index.css`. Match it. Do not introduce a component library.
4. **View-model logic lives in pure modules under `frontend/src/lib/`**, unit-tested — `consentModel.ts`, `taskFlow.ts`, `roomModel.ts`, `wikiModel.ts`, `format.ts`. Rendering decisions go there, not inline in components.
5. **Frontend suites run with `--no-file-parallelism`** on this machine; vitest worker forks crash under Docker contention.

---

## D5: The product works on a phone

**The dominant gap.** Zero breakpoints exist today.

**Files:**
- Modify: `frontend/src/index.css` (breakpoint tokens + the one media query layer)
- Modify: `frontend/src/components/Sidebar.tsx` (the hard `width: 250`)
- Modify: `frontend/src/App.tsx` (`TeamShell`'s flex row)
- Modify: `frontend/src/screens/GroupRoom.tsx` (rails at `width: 296` / `width: 364`)
- Modify: `frontend/src/screens/{Tasks,Wiki,Documents,ConsentInbox,PrivateThread,Setup,TeamGate}.tsx` as each needs
- Create: `frontend/src/lib/layout.ts` + `frontend/tests/unit/layout.test.ts`

**The approach, and why it is not a rewrite.** The layout is inline-styled with fixed pixel widths. Rather than converting every screen to CSS modules, add:

1. a single **breakpoint token** and a `useIsNarrow()` hook (or a CSS-only equivalent) in `frontend/src/lib/layout.ts`, unit-tested;
2. an **off-canvas sidebar** below the breakpoint — hidden by default, opened by a header control, closing on navigation;
3. rails that **stack below the conversation** rather than beside it;
4. `max-width: 100%` and `min-width: 0` discipline so nothing forces horizontal scroll.

- [ ] **Step 1: pin the failure first.** Write a test that fails on the current layout — e.g. a component test at 375px asserting the sidebar is not rendered inline, or a Playwright check that `document.body.scrollWidth <= window.innerWidth`. **A responsive change with no failing test is unverifiable by anything but eyes.**
- [ ] **Step 2:** run it, confirm red.
- [ ] **Step 3:** implement, screen by screen, smallest diff that holds.
- [ ] **Step 4: verify in the browser at 375px, 768px and desktop.** Screenshot each. The horizontal-scroll check is the objective gate; the screenshots are the judgement one.
- [ ] **Step 5:** `npm run build && npx vitest run --no-file-parallelism && npm run test:integration && npx playwright test`.
- [ ] **Step 6:** commit.

---

## D3: ~~Someone is told a decision is waiting~~ — RE-SCOPED 2026-08-31

**Most of this item does not exist.** Checked against the running app and the
live RLS policies:

- The pending count is already surfaced in **three** places in the sidebar —
  the agent card, a badge on the inbox nav item, and Live signals — all
  live-updating through realtime. Every card already shows an expiry
  countdown. "Currently invisible" was wrong.
- `au_consent_queue_select` is `requesting_member_id = auth.uid()`, so a
  consent item is only ever visible to, and actionable by, **the member who
  asked for it**. There is no case of a proposal sitting unseen by someone who
  needed to know: the only possible recipient was in the app moments earlier,
  asking for it. An email would remind you about a thing you just requested.

**What was actually broken, and is now fixed** (commit `9179129`): `status`
stays `'pending'` forever because nothing sweeps the queue, so an expired item
sat in Pending, counted toward "awaiting your key", and offered an APPROVE
button that `execute_consent` refuses with a 409.

**Deferred, not done:** outbound email. Revisit if a real trigger appears —
proposals from an autonomous producer (which §12 proves does not exist), or a
second member gaining visibility of someone else's queue. Neither is true
today, and building an SMTP pipeline for a self-reminder is the wrong order.

### Original scope, kept for the reasoning


Proposals sit for a 7-day TTL and **nothing tells anyone**. The consent protocol is the product's governance thesis and it is currently invisible unless a member happens to open the inbox.

**Files:**
- Migration: a `notifications` table, or a `notified_at` column on `consent_queue` — decide and state which
- Modify: `shared/consent.py`, `pipeline/worker.py` (a digest job), `server/app.py`
- Modify: `frontend/src/components/Sidebar.tsx` (unread count)

- [ ] Sidebar unread count — the cheapest half, entirely client-side from data already fetched.
- [ ] An email on a new proposal, and a daily digest of what is still pending. Supabase's GoTrue already sends invite mail (`server/invites.py`); reuse that path rather than adding a provider.
- [ ] **Any new table needs an explicit `revoke` from `anon` and `authenticated`** — Supabase's default grant has mattered three times in this codebase already.
- [ ] Respect the expiry: a digest must not nag about an item that has expired.

---

## D4: A team is a thing you can leave

No leave, no remove-member, no delete-team, no export exists anywhere.

**The design constraint that makes this interesting.** §23.1: *"nobody approves another member's actions, nobody configures permissions."* So:

- **Leave** is self-service. A member removes themselves; nobody approves it.
- **Remove another member** is *not* an admin power — it must route through the consent protocol, and the affected member's own key is the one that matters. Model it on the existing tier reasoning: it affects one member, so it is T1.
- **Export** is member-initiated and returns the team's data — the wiki, tasks, messages the member can see. It is also the honest answer to "what happens if we stop paying" (§23.3: memory and history are retained).
- **Delete team** is the destructive one. Follow the codebase's own rule: *deletion leaves a trace*. Consider soft-delete over `drop`.

---

## D2: Empty and error states, the remaining gap

Smaller than budgeted. The work is a **pass**, not a build:

- [ ] Audit all seven screens; fill the ones without an empty state.
- [ ] **Distinguish "no data" from "the request failed."** The hooks already carry `error`; most screens render an empty state either way, so a member whose network dropped is told the team has no tasks. That is the actual defect here, and it is a correctness one.
- [ ] Keep the copy in the established voice: factual, next-action-oriented, no apology.

---

## D1: First-run and wiki bootstrap

§6.3-9 records "no bootstrap — the wiki starts empty" as a memory gap and never scheduled it.

- [ ] A guided first-run: ask for one document or one repository, run the compile in the foreground, land the member on the diff card their own upload produced. The moment a team sees Comrade turn *their* document into cited facts is the moment the product explains itself.
- [ ] Everything it needs already exists — document upload, the compiler, diff cards, and (since Phase 3) repo ingestion.

---

## D6: The turn shows its work

Partly built — `GroupRoom.tsx` already consumes `streamTurn`'s frames.

- [ ] Verify what is actually rendered today before writing anything.
- [ ] The runtime emits every `tool_call` and `tool_result`; showing them turns a spinner into "reading the wiki… searching the room…", which is both faster-feeling and more honest.
- [ ] Phase 1 added a `busy` frame for a room whose turn lock is held (decision Q6) — make sure that reaches the member as its honest line rather than a silent no-op.

---

## Track D exit criteria

- [ ] No horizontal scroll at 375px on any screen; verified in a real browser and pinned by a test
- [ ] A pending consent proposal produces a visible unread signal, and an email
- [ ] A member can leave a team without asking anyone
- [ ] Every screen distinguishes "nothing here yet" from "this failed to load"
- [ ] `uv run pytest` green; frontend build, unit, integration, e2e green
- [ ] `graphify` refreshed; branch merged to `master`
