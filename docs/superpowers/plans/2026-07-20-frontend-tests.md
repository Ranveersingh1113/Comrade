# Frontend Test Suite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Four-layer test suite (pure logic / component / real-DB RLS integration / thin Playwright E2E) for `frontend/`, per `docs/superpowers/specs/2026-07-20-frontend-tests-design.md`.

**Architecture:** Extract untestable view-model logic out of oversized screens into `src/lib/*.ts` modules first; test those pure. Component layer mocks `lib/supabase` at the module boundary and fakes the FastAPI surface with MSW. Integration layer runs supabase-js as real GoTrue-authenticated users against the local stack. E2E drives the real app with Playwright, including a two-browser-context T3 countersign.

**Tech Stack:** Vitest 3, @testing-library/react 16, jsdom, MSW 2, @playwright/test, pg (dev-only).

## Global Constraints

- All work stays inside `frontend/` except this plan file's checkboxes.
- Never use `service_role` or the sb_secret key in any file under `frontend/src/` — test fixtures under `frontend/tests/` may use `COMRADE_DB_URL_ADMIN` and GoTrue admin, dev-only.
- Behavior-preserving extractions: screens must render the same DOM (build + existing manual smoke unchanged).
- Conventional commits, no attribution trailers.
- `npm run build` and `npm test` green at every commit.
- Coverage: v8, 80% lines on `src/lib/**` (screens/components earn theirs via layers 2/4).
- Integration/E2E files must skip cleanly (not fail) when the local stack is down: probe once in global setup and `console.warn` + skip.
- Test users for layers 3/4 use `fe-*@test.dev` emails; every suite cleans up its own teams/users in teardown.

---

### Task 1: Test infrastructure + format.ts unit tests

**Files:**
- Modify: `frontend/package.json` (deps + scripts)
- Create: `frontend/vitest.config.ts`
- Create: `frontend/tests/setup.ts`
- Test: `frontend/tests/unit/format.test.ts`

**Interfaces:**
- Consumes: `src/lib/format.ts` exports (`avatarColors`, `initialsOf`, `firstNameOf`, `messageTime`, `shortDate`, `daysUntil`, `countdown`, `shortHash`)
- Produces: `npm test` script running Vitest with jsdom + setup file; later tasks add tests under `frontend/tests/unit/` and `frontend/tests/component/`.

- [ ] **Step 1: Install dev-deps**

```bash
cd frontend
npm i -D vitest @vitest/coverage-v8 @testing-library/react @testing-library/user-event @testing-library/jest-dom jsdom msw
```

- [ ] **Step 2: Create vitest.config.ts**

```typescript
// frontend/vitest.config.ts
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./tests/setup.ts'],
    include: ['tests/unit/**/*.test.ts', 'tests/component/**/*.test.tsx'],
    coverage: {
      provider: 'v8',
      include: ['src/lib/**'],
      thresholds: { lines: 80 },
    },
  },
});
```

- [ ] **Step 3: Create tests/setup.ts** (env stubs BEFORE any src import; jest-dom matchers)

```typescript
// frontend/tests/setup.ts
import '@testing-library/jest-dom/vitest';
import { vi } from 'vitest';

// src/lib/supabase.ts throws without these; component tests mock the module,
// but anything importing it transitively still needs the env present.
vi.stubEnv('VITE_SUPABASE_URL', 'http://127.0.0.1:54321');
vi.stubEnv('VITE_SUPABASE_ANON_KEY', 'test-anon-key');
vi.stubEnv('VITE_AGENT_API_URL', 'http://localhost:8000');
```

- [ ] **Step 4: Add scripts to package.json** (`test`, `test:coverage`)

```json
"test": "vitest run",
"test:watch": "vitest",
"test:coverage": "vitest run --coverage"
```

- [ ] **Step 5: Write the failing test file**

```typescript
// frontend/tests/unit/format.test.ts
import { describe, expect, test, vi, beforeEach, afterEach } from 'vitest';
import {
  avatarColors, countdown, daysUntil, firstNameOf, initialsOf,
  messageTime, shortHash,
} from '../../src/lib/format';

describe('initialsOf', () => {
  test('two names -> first+last initials', () => {
    expect(initialsOf('Priya Sharma')).toBe('PS');
  });
  test('single name -> first two letters', () => {
    expect(initialsOf('Marcus')).toBe('MA');
  });
  test('empty -> ?', () => {
    expect(initialsOf('   ')).toBe('?');
  });
  test('three names -> first and LAST initial', () => {
    expect(initialsOf('Ana de Souza')).toBe('AS');
  });
});

describe('firstNameOf', () => {
  test('takes the first word', () => {
    expect(firstNameOf('Priya Sharma')).toBe('Priya');
  });
});

describe('avatarColors', () => {
  test('deterministic for the same id', () => {
    expect(avatarColors('user-1')).toEqual(avatarColors('user-1'));
  });
  test('returns a palette entry with bg and fg', () => {
    const c = avatarColors('anything');
    expect(c.bg).toMatch(/^#/);
    expect(c.fg).toMatch(/^#/);
  });
});

describe('time formatting (frozen clock)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-20T15:00:00'));
  });
  afterEach(() => vi.useRealTimers());

  test('messageTime today -> bare clock', () => {
    expect(messageTime('2026-07-20T09:15:00')).not.toMatch(/^YD|[A-Z]{3}/);
  });
  test('messageTime yesterday -> YD prefix', () => {
    expect(messageTime('2026-07-19T18:12:00')).toMatch(/^YD /);
  });
  test('messageTime older -> UPPER month-day', () => {
    expect(messageTime('2026-07-11T10:00:00')).toMatch(/^[A-Z]{3} \d+$/);
  });
  test('daysUntil clamps past dates to 0', () => {
    expect(daysUntil('2026-07-01T00:00:00')).toBe(0);
  });
  test('daysUntil counts up', () => {
    expect(daysUntil('2026-07-23T15:00:00')).toBe(3);
  });
  test('countdown formats days+hours', () => {
    expect(countdown('2026-07-27T12:00:00')).toBe('6D 21H');
  });
  test('countdown past -> EXPIRED', () => {
    expect(countdown('2026-07-19T00:00:00')).toBe('EXPIRED');
  });
});

describe('shortHash', () => {
  test('long hash -> 4…4 with prefix stripped', () => {
    expect(shortHash('sha256:abcdef1234567890')).toBe('abcd…7890');
  });
  test('short hash unchanged', () => {
    expect(shortHash('abc123')).toBe('abc123');
  });
});
```

- [ ] **Step 6: Run** `npm test` — expect all format tests PASS (format.ts already exists; this task is infrastructure-proving). If any fail, the test encodes a real behavior question — fix the TEST only if it misread the source, never weaken format.ts.

- [ ] **Step 7: Commit** `test: vitest infrastructure + format unit tests`

---

### Task 2: Extract lib/taskFlow.ts + tests, refactor Tasks screen

**Files:**
- Create: `frontend/src/lib/taskFlow.ts`
- Modify: `frontend/src/hooks/useTasks.ts` (move `NEXT`, `taskCell`, `taskPill`, `taskMark` out; re-export for compat)
- Modify: `frontend/src/screens/Tasks.tsx:216-271` (use `taskAffordance`)
- Test: `frontend/tests/unit/taskFlow.test.ts`

**Interfaces:**
- Produces:
```typescript
export type TaskAffordance =
  | { kind: 'confirm' }         // assignee, proposed  -> "CONFIRM — IT'S YOURS"
  | { kind: 'start' }           // assignee, confirmed -> "START"
  | { kind: 'finish' }          // assignee, in_progress -> "MARK DONE"
  | { kind: 'wait'; assigneeName: string }  // non-assignee, proposed
  | { kind: 'none' };           // done, or non-assignee non-proposed
export function taskAffordance(task: Pick<Task, 'status' | 'assignee_id'>, viewerId: string | null, assigneeName: string): TaskAffordance;
export function nextStatus(status: TaskStatus): TaskStatus | null;   // proposed->confirmed->in_progress->done->null
export function confirmPatch(next: TaskStatus): Partial<Task>;       // {status} + confirmed_at iff next==='confirmed'
export { taskCell, taskPill, taskMark };  // moved verbatim from useTasks.ts
```

- [ ] **Step 1: Write failing tests**

```typescript
// frontend/tests/unit/taskFlow.test.ts
import { describe, expect, test } from 'vitest';
import { confirmPatch, nextStatus, taskAffordance, taskMark, taskPill } from '../../src/lib/taskFlow';

const t = (status: string, assignee: string | null = 'u1') =>
  ({ status, assignee_id: assignee }) as never;

describe('taskAffordance — mirrors the DB assignee-confirm trigger', () => {
  test('assignee sees confirm on proposed', () => {
    expect(taskAffordance(t('proposed'), 'u1', 'Priya')).toEqual({ kind: 'confirm' });
  });
  test('assignee sees start on confirmed', () => {
    expect(taskAffordance(t('confirmed'), 'u1', 'Priya')).toEqual({ kind: 'start' });
  });
  test('assignee sees finish on in_progress', () => {
    expect(taskAffordance(t('in_progress'), 'u1', 'Priya')).toEqual({ kind: 'finish' });
  });
  test('NON-assignee sees wait copy on proposed — never a button', () => {
    expect(taskAffordance(t('proposed'), 'u2', 'Priya')).toEqual({
      kind: 'wait', assigneeName: 'Priya',
    });
  });
  test('non-assignee sees nothing on other statuses', () => {
    expect(taskAffordance(t('in_progress'), 'u2', 'Priya')).toEqual({ kind: 'none' });
  });
  test('done shows nothing even for the assignee', () => {
    expect(taskAffordance(t('done'), 'u1', 'Priya')).toEqual({ kind: 'none' });
  });
  test('signed-out viewer never gets a button', () => {
    expect(taskAffordance(t('proposed'), null, 'Priya')).toEqual({
      kind: 'wait', assigneeName: 'Priya',
    });
  });
});

describe('nextStatus / confirmPatch', () => {
  test('lifecycle chain', () => {
    expect(nextStatus('proposed')).toBe('confirmed');
    expect(nextStatus('confirmed')).toBe('in_progress');
    expect(nextStatus('in_progress')).toBe('done');
    expect(nextStatus('done')).toBeNull();
  });
  test('confirming stamps confirmed_at; other transitions do not', () => {
    expect(confirmPatch('confirmed').confirmed_at).toBeTruthy();
    expect(confirmPatch('done').confirmed_at).toBeUndefined();
  });
});

describe('presentation maps stay total', () => {
  test.each(['proposed', 'confirmed', 'in_progress', 'done'] as const)('%s', (s) => {
    expect(taskPill(s).label).toBeTruthy();
    expect(typeof taskMark(s)).toBe('string');
  });
});
```

- [ ] **Step 2: Run** `npx vitest run tests/unit/taskFlow.test.ts` — FAIL (module missing).
- [ ] **Step 3: Implement `src/lib/taskFlow.ts`** — move `NEXT`/`taskCell`/`taskPill`/`taskMark` from `useTasks.ts` verbatim, add:

```typescript
import type { Task, TaskStatus } from './types';

const NEXT: Partial<Record<TaskStatus, TaskStatus>> = {
  proposed: 'confirmed', confirmed: 'in_progress', in_progress: 'done',
};

export function nextStatus(status: TaskStatus): TaskStatus | null {
  return NEXT[status] ?? null;
}

export function confirmPatch(next: TaskStatus): Partial<Task> {
  const patch: Partial<Task> = { status: next };
  if (next === 'confirmed') patch.confirmed_at = new Date().toISOString();
  return patch;
}

export type TaskAffordance =
  | { kind: 'confirm' } | { kind: 'start' } | { kind: 'finish' }
  | { kind: 'wait'; assigneeName: string } | { kind: 'none' };

/** Which control the viewer gets. Mirrors (never replaces) the DB trigger. */
export function taskAffordance(
  task: Pick<Task, 'status' | 'assignee_id'>,
  viewerId: string | null,
  assigneeName: string,
): TaskAffordance {
  const mine = viewerId !== null && task.assignee_id === viewerId;
  if (mine) {
    if (task.status === 'proposed') return { kind: 'confirm' };
    if (task.status === 'confirmed') return { kind: 'start' };
    if (task.status === 'in_progress') return { kind: 'finish' };
    return { kind: 'none' };
  }
  if (task.status === 'proposed') return { kind: 'wait', assigneeName };
  return { kind: 'none' };
}
```
Then in `useTasks.ts`: import `nextStatus`, `confirmPatch` and use them in `advance` (`const next = nextStatus(task.status); if (!next) return; const patch = confirmPatch(next);`); delete the local `NEXT`/`taskCell`/`taskPill`/`taskMark` and re-export: `export { taskCell, taskPill, taskMark } from '../lib/taskFlow';`. In `Tasks.tsx` replace the four inline `isMine && status===…` blocks with a `switch (taskAffordance(t, myUserId, profile.display_name).kind)` render keeping the SAME button text and classNames.

- [ ] **Step 4: Run** `npx vitest run tests/unit/taskFlow.test.ts && npm run build` — PASS + clean build.
- [ ] **Step 5: Commit** `refactor: extract taskFlow view-model + unit tests`

---

### Task 3: Extract lib/roomModel.ts + tests, refactor GroupRoom fragments

**Files:**
- Create: `frontend/src/lib/roomModel.ts`
- Modify: `frontend/src/screens/GroupRoom.tsx` (MessageRow ~254-376 uses `classifyMessage`; useMemberBars ~378-395 uses `memberBars`)
- Test: `frontend/tests/unit/roomModel.test.ts`

**Interfaces:**
- Produces:
```typescript
export type MessageKind = 'deleted' | 'ai' | 'user';
export interface ClassifiedMessage { kind: MessageKind; isDiffCard: boolean; }
/** diffCardIds = message ids referenced by memory_compilations.diff_message_id */
export function classifyMessage(m: Message, diffCardIds: ReadonlySet<string>): ClassifiedMessage;
export interface MemberBar { userId: string; name: string; tasks: Task[]; doneCount: number; }
export function memberBars(roster: Array<{ profile: Profile }>, tasks: Task[]): MemberBar[];
```

- [ ] **Step 1: Write failing tests**

```typescript
// frontend/tests/unit/roomModel.test.ts
import { describe, expect, test } from 'vitest';
import { classifyMessage, memberBars } from '../../src/lib/roomModel';
import type { Message, Profile, Task } from '../../src/lib/types';

const msg = (over: Partial<Message>): Message => ({
  id: 'm1', team_id: 't1', thread_type: 'group', thread_owner_id: null,
  sender_kind: 'user', sender_id: 'u1', body: 'hello', deleted_scope: null,
  deleted_by: null, deleted_at: null, created_at: '2026-07-20T10:00:00Z',
  ...over,
});

describe('classifyMessage', () => {
  test('deleted-for-everyone wins over everything (trace, not gap)', () => {
    const c = classifyMessage(
      msg({ deleted_scope: 'everyone', sender_kind: 'ai', sender_id: null }),
      new Set(['m1']),
    );
    expect(c.kind).toBe('deleted');
    expect(c.isDiffCard).toBe(false);
  });
  test('ai message', () => {
    expect(classifyMessage(msg({ sender_kind: 'ai', sender_id: null }), new Set()).kind).toBe('ai');
  });
  test('diff card = AI message pointed at by a compilation', () => {
    const c = classifyMessage(msg({ sender_kind: 'ai', sender_id: null }), new Set(['m1']));
    expect(c).toEqual({ kind: 'ai', isDiffCard: true });
  });
  test('a USER message id in diffCardIds is not a diff card', () => {
    expect(classifyMessage(msg({}), new Set(['m1'])).isDiffCard).toBe(false);
  });
});

describe('memberBars', () => {
  const prof = (id: string, name: string) => ({ profile: { id, display_name: name } as Profile });
  const task = (id: string, assignee: string | null, status: Task['status']): Task =>
    ({ id, assignee_id: assignee, status } as Task);

  test('groups tasks per member and counts done', () => {
    const bars = memberBars(
      [prof('u1', 'Priya'), prof('u2', 'Marcus')],
      [task('t1', 'u1', 'done'), task('t2', 'u1', 'proposed'), task('t3', 'u2', 'in_progress')],
    );
    expect(bars).toHaveLength(2);
    expect(bars[0]).toMatchObject({ userId: 'u1', name: 'Priya', doneCount: 1 });
    expect(bars[0].tasks.map((t) => t.id)).toEqual(['t1', 't2']);
  });
  test('unassigned tasks appear in nobody's bar; empty roster -> []', () => {
    expect(memberBars([], [task('t1', null, 'proposed')])).toEqual([]);
  });
});
```

- [ ] **Step 2: Run — FAIL.** Then implement `src/lib/roomModel.ts` to satisfy exactly, moving GroupRoom's inline logic (the `deleted_scope === 'everyone'` branch, AI/diff detection, and `useMemberBars` computation) into these pure functions; GroupRoom keeps its rendering but delegates classification. `useMemberBars` becomes a thin `useMemo(() => memberBars(roster, taskState.tasks), …)`.

```typescript
// frontend/src/lib/roomModel.ts
import type { Message, Profile, Task } from './types';

export type MessageKind = 'deleted' | 'ai' | 'user';

export interface ClassifiedMessage {
  kind: MessageKind;
  isDiffCard: boolean;
}

/** Deletion always renders as a trace — never a silent gap — so it wins. */
export function classifyMessage(
  m: Message,
  diffCardIds: ReadonlySet<string>,
): ClassifiedMessage {
  if (m.deleted_scope === 'everyone') return { kind: 'deleted', isDiffCard: false };
  if (m.sender_kind === 'ai') return { kind: 'ai', isDiffCard: diffCardIds.has(m.id) };
  return { kind: 'user', isDiffCard: false };
}

export interface MemberBar {
  userId: string;
  name: string;
  tasks: Task[];
  doneCount: number;
}

export function memberBars(
  roster: ReadonlyArray<{ profile: Profile }>,
  tasks: ReadonlyArray<Task>,
): MemberBar[] {
  return roster.map(({ profile }) => {
    const mine = tasks.filter((t) => t.assignee_id === profile.id);
    return {
      userId: profile.id,
      name: profile.display_name,
      tasks: mine,
      doneCount: mine.filter((t) => t.status === 'done').length,
    };
  });
}
```

- [ ] **Step 3: Run tests + build — PASS.**
- [ ] **Step 4: Commit** `refactor: extract roomModel (classification, member bars) + tests`

---

### Task 4: Extract lib/wikiModel.ts + tests; fix MemoryChangeType drift

**Files:**
- Create: `frontend/src/lib/wikiModel.ts`
- Modify: `frontend/src/lib/types.ts:93` — `MemoryChangeType` must be `'added' | 'revised' | 'invalidated' | 'reverted'` (backend added `invalidated` in compiler-v2; frontend type predates it)
- Modify: `frontend/src/screens/Wiki.tsx` (local `Fact`/`PageView` interfaces + projection replaced by wikiModel)
- Test: `frontend/tests/unit/wikiModel.test.ts`

**Interfaces:**
- Produces:
```typescript
export interface WikiFact {
  entryId: string; versionId: string; fact: string;
  citations: MemoryCitation[]; history: MemoryVersion[];  // newest first, active excluded
  revertQueued: boolean;
}
export interface WikiPage { pageId: string | null; title: string; description: string; facts: WikiFact[]; }
export const UNCATEGORIZED = 'Uncategorized';
export function projectWiki(
  pages: MemoryPage[], entries: MemoryEntry[], versions: MemoryVersion[],
  citations: MemoryCitation[], reverts: MemoryRevert[],
): WikiPage[];   // pages in title order, Uncategorized last, empty pages dropped
```

- [ ] **Step 1: Write failing tests**

```typescript
// frontend/tests/unit/wikiModel.test.ts
import { describe, expect, test } from 'vitest';
import { projectWiki, UNCATEGORIZED } from '../../src/lib/wikiModel';
import type {
  MemoryCitation, MemoryEntry, MemoryPage, MemoryRevert, MemoryVersion,
} from '../../src/lib/types';

const page = (id: string, title: string): MemoryPage =>
  ({ id, title, description: '', team_id: 't1' } as MemoryPage);
const entry = (id: string, page_id: string | null): MemoryEntry =>
  ({ id, page_id, team_id: 't1' } as MemoryEntry);
const ver = (id: string, entry_id: string, fact: string, is_active: boolean, created_at: string): MemoryVersion =>
  ({ id, entry_id, fact, is_active, created_at, change_type: 'added' } as MemoryVersion);

test('facts group under their pages; page-less land in Uncategorized last', () => {
  const out = projectWiki(
    [page('p1', 'Deadlines')],
    [entry('e1', 'p1'), entry('e2', null)],
    [ver('v1', 'e1', 'Demo Friday', true, '1'), ver('v2', 'e2', 'Stray fact', true, '1')],
    [], [],
  );
  expect(out.map((p) => p.title)).toEqual(['Deadlines', UNCATEGORIZED]);
  expect(out[0].facts[0].fact).toBe('Demo Friday');
  expect(out[1].facts[0].fact).toBe('Stray fact');
});

test('empty pages are dropped; only ACTIVE versions become facts', () => {
  const out = projectWiki(
    [page('p1', 'Deadlines'), page('p2', 'Empty Page')],
    [entry('e1', 'p1')],
    [ver('v1', 'e1', 'Old', false, '1'), ver('v2', 'e1', 'Current', true, '2')],
    [], [],
  );
  expect(out).toHaveLength(1);
  expect(out[0].facts).toHaveLength(1);
  expect(out[0].facts[0].fact).toBe('Current');
});

test('history: inactive versions newest-first; citations attach to the active version', () => {
  const cite = { id: 'c1', version_id: 'v3', source_kind: 'message', source_id: 'm1' } as MemoryCitation;
  const out = projectWiki(
    [page('p1', 'P')], [entry('e1', 'p1')],
    [ver('v1', 'e1', 'a', false, '1'), ver('v2', 'e1', 'b', false, '2'), ver('v3', 'e1', 'c', true, '3')],
    [cite], [],
  );
  const f = out[0].facts[0];
  expect(f.history.map((h) => h.fact)).toEqual(['b', 'a']);
  expect(f.citations).toEqual([cite]);
});

test('a revert row marks the fact revertQueued', () => {
  const out = projectWiki(
    [page('p1', 'P')], [entry('e1', 'p1')],
    [ver('v1', 'e1', 'x', true, '1')],
    [], [{ entry_id: 'e1' } as MemoryRevert],
  );
  expect(out[0].facts[0].revertQueued).toBe(true);
});
```

- [ ] **Step 2: Run — FAIL.** Implement `wikiModel.ts` (move Wiki.tsx's projection; keep title-sort, Uncategorized-last, empty-dropped semantics that HANDOFF §5.3 and `pipeline/wiki.py` define). Fix `types.ts` `MemoryChangeType`.
- [ ] **Step 3: Run tests + build — PASS.** Wiki.tsx renders from `projectWiki` output.
- [ ] **Step 4: Commit** `refactor: extract wikiModel + fix MemoryChangeType drift`

---

### Task 5: Extract lib/consentModel.ts + tests; add tier columns to types

**Files:**
- Create: `frontend/src/lib/consentModel.ts`
- Modify: `frontend/src/lib/types.ts` — `ConsentItem` gains `tier: 'T0'|'T1'|'T2'|'T3'`, `second_approver_id: string | null`, `second_approved_at: string | null` (migration 20260719130000)
- Modify: `frontend/src/components/ConsentCard.tsx` (state machine from model; render unchanged)
- Test: `frontend/tests/unit/consentModel.test.ts`

**Interfaces:**
- Produces:
```typescript
export type ConsentPhase =
  | 'pending'                 // actionable by requester
  | 'awaiting_second_key'     // T3, requester approved, no countersign
  | 'countersigned_pending'   // T3, countersigned, requester not yet approved
  | 'can_countersign'         // T3, viewer is a teammate (not requester), no countersign yet
  | 'executed' | 'rejected' | 'cancelled' | 'stale';
export function consentPhase(item: ConsentItem, viewerId: string | null, staleFromApi: boolean): ConsentPhase;
export function argsPretty(args: Record<string, unknown>): string;  // stable-ordered JSON, 2-space
```

- [ ] **Step 1: Write failing tests**

```typescript
// frontend/tests/unit/consentModel.test.ts
import { describe, expect, test } from 'vitest';
import { argsPretty, consentPhase } from '../../src/lib/consentModel';
import type { ConsentItem } from '../../src/lib/types';

const item = (over: Partial<ConsentItem>): ConsentItem => ({
  id: 'c1', team_id: 't1', requesting_member_id: 'u1', tool_name: 'post_group_message',
  tool_args: { body: 'hi' }, source_snippet: null, action_hash: 'h', status: 'pending',
  reversible: true, expires_at: null, created_at: '1', resolved_at: null,
  tier: 'T2', second_approver_id: null, second_approved_at: null,
  ...over,
});

describe('consentPhase', () => {
  test('T2 pending, requester viewing -> pending', () => {
    expect(consentPhase(item({}), 'u1', false)).toBe('pending');
  });
  test('stale from API wins over stored status', () => {
    expect(consentPhase(item({}), 'u1', true)).toBe('stale');
  });
  test('T3 approved without countersign -> awaiting_second_key', () => {
    expect(consentPhase(item({ tier: 'T3', status: 'approved' }), 'u1', false))
      .toBe('awaiting_second_key');
  });
  test('T3 pending viewed by a TEAMMATE -> can_countersign', () => {
    expect(consentPhase(item({ tier: 'T3' }), 'u2', false)).toBe('can_countersign');
  });
  test('T3 countersigned but not approved -> countersigned_pending for requester', () => {
    expect(consentPhase(item({ tier: 'T3', second_approver_id: 'u2' }), 'u1', false))
      .toBe('countersigned_pending');
  });
  test('teammate who already countersigned sees countersigned_pending, not can_countersign', () => {
    expect(consentPhase(item({ tier: 'T3', second_approver_id: 'u2' }), 'u2', false))
      .toBe('countersigned_pending');
  });
  test('executed/rejected pass through', () => {
    expect(consentPhase(item({ status: 'executed' }), 'u1', false)).toBe('executed');
    expect(consentPhase(item({ status: 'rejected' }), 'u1', false)).toBe('rejected');
  });
});

describe('argsPretty', () => {
  test('stable key order regardless of insertion order', () => {
    expect(argsPretty({ b: 1, a: 2 })).toBe(argsPretty({ a: 2, b: 1 }));
  });
  test('renders literal values (hard design rule: show EXACTLY what will run)', () => {
    expect(argsPretty({ body: 'post this' })).toContain('"body": "post this"');
  });
});
```

- [ ] **Step 2: Run — FAIL. Implement:**

```typescript
// frontend/src/lib/consentModel.ts
import type { ConsentItem } from './types';

export type ConsentPhase =
  | 'pending' | 'awaiting_second_key' | 'countersigned_pending'
  | 'can_countersign' | 'executed' | 'rejected' | 'cancelled' | 'stale';

export function consentPhase(
  item: ConsentItem,
  viewerId: string | null,
  staleFromApi: boolean,
): ConsentPhase {
  if (staleFromApi) return 'stale';
  if (item.status === 'executed') return 'executed';
  if (item.status === 'rejected') return 'rejected';
  if (item.status === 'cancelled') return 'cancelled';

  const isRequester = viewerId !== null && item.requesting_member_id === viewerId;
  if (item.tier === 'T3') {
    const countersigned = item.second_approver_id !== null;
    if (item.status === 'approved' && !countersigned) return 'awaiting_second_key';
    if (countersigned && item.status === 'pending') return 'countersigned_pending';
    if (!countersigned && !isRequester) return 'can_countersign';
  }
  return 'pending';
}

/** Literal args, stable order — the card must show EXACTLY what will run. */
export function argsPretty(args: Record<string, unknown>): string {
  const ordered = Object.fromEntries(
    Object.entries(args).sort(([a], [b]) => a.localeCompare(b)),
  );
  return JSON.stringify(ordered, null, 2);
}
```
Refactor ConsentCard.tsx to derive its branches from `consentPhase` (keep the existing stale copy string exactly).

- [ ] **Step 3: Run tests + build — PASS.**
- [ ] **Step 4: Commit** `refactor: extract consentModel + tier columns in types`

---

### Task 6: Component tests — ConsentCard + MemoryDiffCard (MSW + mocked supabase)

**Files:**
- Create: `frontend/tests/component/mocks.tsx` (shared: supabase module mock factory, MSW server, TeamContext/AuthContext wrappers)
- Test: `frontend/tests/component/ConsentCard.test.tsx`
- Test: `frontend/tests/component/MemoryDiffCard.test.tsx`

**Interfaces:**
- Consumes: ConsentCard props (read its file for the exact prop shape before writing; it takes the consent item + team id), MemoryDiffCard props, `lib/agentApi` endpoint paths.
- Produces: `renderWithTeam(ui, {myUserId, teamId})` helper reused by Task 7.

Key assertions (write with Testing Library queries, user-event for clicks; MSW `http.post` handlers on `http://localhost:8000/...`):

- ConsentCard renders the **literal tool name** (`post_group_message`), the **literal args JSON** (`"body": "…"`), and the **source snippet** text.
- APPROVE → MSW receives `/consent/{id}/approve` with team_id; card flips to executed copy.
- MSW returns **409** → the stale copy ("went stale") renders; approve/reject controls disappear; distinct from a 404 (renders the not-found/generic error, NOT the stale copy).
- Reject → `/consent/{id}/reject` called.
- T3 item (tier:'T3') viewed by a teammate renders a countersign affordance; viewed by requester after approve renders awaiting-second-key copy.
- MemoryDiffCard: collapsed shows added/revised/removed counts; expand lists versions; clicking revert calls `supabase.from('memory_reverts').insert` with `{entry_id, team_id, member_id: myUserId}` (assert via the mock); reverted row shows queued state.

- [ ] **Step 1: Write mocks.tsx** — `vi.mock('../../src/lib/supabase', …)` exporting a chainable query stub (`from().select().eq().order()` resolving seeded fixtures; `insert` recorded to an array) + `setupServer()` from `msw/node` started in the file's `beforeAll`.
- [ ] **Step 2: Write both test files against the assertions above** (read the two components first; assert on their actual copy strings).
- [ ] **Step 3: Run `npm test` — all green.**
- [ ] **Step 4: Commit** `test: component coverage for consent + memory diff cards`

---

### Task 7: Component tests — Tasks, GroupRoom fragments, Sidebar, ErrorBoundary, PrivateThread

**Files:**
- Test: `frontend/tests/component/Tasks.test.tsx`
- Test: `frontend/tests/component/GroupRoom.test.tsx`
- Test: `frontend/tests/component/shell.test.tsx` (Sidebar + ErrorBoundary)
- Test: `frontend/tests/component/PrivateThread.test.tsx`

Assertions:

- Tasks: with viewer=assignee a proposed task shows `CONFIRM — IT'S YOURS`; viewer=other shows `waiting on … to confirm` and NO button; advancing calls `supabase.from('tasks').update`.
- GroupRoom: message with `deleted_scope='everyone'` renders the deletion placeholder text and NOT the body; AI message shows the AI badge; a compilation-linked AI message renders as diff card.
- ErrorBoundary: child that throws renders the fallback; sibling content stays mounted.
- Sidebar: pending consent count badge renders when items exist; private-thread unread dot logic.
- PrivateThread: renders messages where `thread_owner_id === myUserId`; agent send calls MSW `/agent/turn` and does NOT insert the reply locally (Realtime owns delivery).

Steps: write → run (fix only test-side misreads) → `npm run test:coverage` must pass the 80% lines threshold on `src/lib/**` → commit `test: component coverage for screens + shell`.

---

### Task 8: Integration infra + RLS proofs part 1

**Files:**
- Create: `frontend/vitest.integration.config.ts` (node env, `tests/integration/**/*.test.ts`, `globalSetup: './tests/integration/global-setup.ts'`, testTimeout 15000)
- Create: `frontend/tests/integration/global-setup.ts`
- Create: `frontend/tests/integration/harness.ts`
- Test: `frontend/tests/integration/rls-messages.test.ts`
- Test: `frontend/tests/integration/rls-memory.test.ts`
- Modify: `frontend/package.json` — `"test:integration": "vitest run -c vitest.integration.config.ts"`; dev-deps add `pg`, `@types/pg`, `dotenv`

**Interfaces (harness.ts produces):**
```typescript
export interface TestUser { id: string; email: string; client: SupabaseClient; }
export function adminSql(): Promise<pg.Client>;                       // COMRADE_DB_URL_ADMIN
export function createUser(tag: string): Promise<TestUser>;           // GoTrue signup fe-<tag>-<rand>@test.dev / fixed pw, returns signed-in client (real ES256 session)
export function createTeam(leader: TestUser, members: TestUser[]): Promise<string>; // team + active memberships via adminSql
export function cleanupTeam(teamId: string, users: TestUser[]): Promise<void>;      // delete team + auth users
export function stackUp(): Promise<boolean>;                          // probe; global-setup warns+skips when false
```
global-setup loads `../.env` (repo root) via dotenv for `COMRADE_DB_URL_ADMIN`, and `frontend/.env.local` for the URL/anon key; sets `process.env.STACK_UP` so tests `describe.skipIf`.

RLS proofs part 1 (each test builds its own little team via harness, cleans up in `afterAll`):

1. `rls-messages`: leader posts group message → member's client sees it; member writes private message (`thread_owner_id = self`) → leader's client gets **zero rows** querying private threads; member cannot insert a message `sender_id` ≠ self (error).
2. `rls-memory`: seed page/entry/version via adminSql → member client reads them; member `insert` into `memory_versions` **errors**; member `update` of a version **errors or writes nothing** (`.select()` returns empty — RLS silently filters updates; assert 0 rows changed); `memory_reverts.insert` with own member_id **succeeds**; with the OTHER member's id **errors**.

Steps: infra → probe test (`stackUp` true against running stack) → write both files → `npm run test:integration` green → commit `test: RLS integration layer (messages, memory)`.

---

### Task 9: RLS proofs part 2 — consent visibility, opens summary, Realtime round-trip

**Files:**
- Test: `frontend/tests/integration/rls-consent.test.ts`
- Test: `frontend/tests/integration/rls-opens.test.ts`
- Test: `frontend/tests/integration/realtime.test.ts`

Proofs:

1. `rls-consent`: seed (adminSql) a T2 pending item for the leader → teammate's client selects consent_queue: **does not see it**; seed a T3 pending item → teammate **sees it**; teammate updates ONLY `second_approver_id = self` → succeeds; teammate update touching `tool_args` → **errors** (trigger text "only set the second key"); requester setting `second_approver_id` → **errors** ("cannot set").
2. `rls-opens`: member A opens a doc (`document_opens` insert self row); member B selects `document_opens` → sees only own rows; member B selects `document_opens_summary` → gets `{opens_count: 1, member_count: 2}`; a non-member user gets zero summary rows.
3. `realtime`: member A's client subscribes `postgres_changes` INSERT on `messages` filtered `team_id=eq.<id>`; member B inserts a group message; **event arrives within 5s** (Promise + timeout; retry once; `describe.skipIf(!STACK_UP)`). Second test: B inserts into their PRIVATE thread; A's subscription must NOT receive it within 2s (RLS filters realtime).

Steps: write each file → run `npm run test:integration` → commit `test: RLS consent/opens + realtime round-trip proofs`.

---

### Task 10: Playwright E2E — critical journeys incl. two-context T3

**Files:**
- Create: `frontend/playwright.config.ts` (webServer: [`npm run dev` on 5173, `uv run uvicorn server.app:app` cwd `..` on 8000], baseURL `http://localhost:5173`, one chromium project)
- Create: `frontend/tests/e2e/fixtures.ts` (reuse harness.ts createUser/createTeam through Node — Playwright globalSetup seeds one team: leader `fe-e2e-lead`, member `fe-e2e-mem`, one proposed task for member, one wiki fact, one T2 + one T3 pending consent for leader; writes ids to `tests/e2e/.state.json`)
- Test: `frontend/tests/e2e/journeys.spec.ts`
- Modify: `frontend/package.json` — `"test:e2e": "playwright test"`, dev-dep `@playwright/test`
- Modify: `frontend/src/screens/Login.tsx` — IF it is magic-link-only, add a password fallback form (email+password inputs calling `supabase.auth.signInWithPassword`) so E2E can log in; keep magic-link primary.

Journeys (one spec file, serial):

1. login as leader (password) → team gate shows the seeded team → enter → group room renders roster names.
2. type a message, send → bubble appears (assert via Realtime: no manual reload).
3. member context confirms their proposed task (`CONFIRM — IT'S YOURS` → pill `CONFIRMED`); leader context sees the update arrive.
4. wiki → revert the seeded fact → `REVERT QUEUED` appears.
5. consent inbox → approve the T2 item → executed state; the AI post appears in the room.
6. **T3 two-key**: leader approves T3 → awaiting-second-key copy; member context opens inbox → countersigns → executed; room shows the post.
7. observation suppress: seed an AI observation message via fixture; leader clicks `REMOVE · DON'T DO THIS AGAIN` → placeholder renders.
8. `test.skip(!process.env.GEMINI_API_KEY)` — private thread: send "what tasks are open?" → a non-empty AI reply bubble arrives.

Steps: `npx playwright install chromium` → config + fixtures → spec (use `test.step`, two `browser.newContext()` for 3/6) → `npm run test:e2e` green with stack up → commit `test: playwright e2e journeys incl. T3 two-key`.

---

## Self-review notes

- Spec coverage: L1 = Tasks 1-5; L2 = Tasks 6-7; L3 = Tasks 8-9; L4 = Task 10; type-drift fixes folded into 4-5. Coverage gate wired in Task 7.
- Tasks 6/7/10 direct the executor to read component files for exact copy strings before asserting — the executor has file access; prop shapes must be read, not invented.
- Login password fallback (Task 10) is the only `src/` change beyond extractions; it is additive and E2E-motivated.
