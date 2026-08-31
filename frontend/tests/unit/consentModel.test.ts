import { describe, expect, it, test } from 'vitest';
import {
  argsPretty, batchProgressLabel, consentPhase, isActionable, isExpired, pendingQueueRows,
} from '../../src/lib/consentModel';
import type { ConsentItem } from '../../src/lib/types';

const item = (over: Partial<ConsentItem>): ConsentItem => ({
  id: 'c1', team_id: 't1', requesting_member_id: 'u1', tool_name: 'task_create',
  tool_args: { body: 'hi' }, source_snippet: null, action_hash: 'h', status: 'pending',
  reversible: true, expires_at: null, created_at: '1', resolved_at: null,
  tier: 'T2', batch_id: null,
  ...over,
});

describe('consentPhase', () => {
  test('T2 pending, requester viewing -> pending', () => {
    expect(consentPhase(item({}), 'u1', false)).toBe('pending');
  });
  test('stale from API wins over stored status', () => {
    expect(consentPhase(item({}), 'u1', true)).toBe('stale');
  });
  test('executed / rejected / cancelled pass through', () => {
    expect(consentPhase(item({ status: 'executed' }), 'u1', false)).toBe('executed');
    expect(consentPhase(item({ status: 'rejected' }), 'u1', false)).toBe('rejected');
    expect(consentPhase(item({ status: 'cancelled' }), 'u1', false)).toBe('cancelled');
  });
  test('a teammate viewing another member\'s item still just sees pending (findings §10)', () => {
    // T3 and its extra approval step are gone — every item needs exactly one
    // key, the requester's. A teammate gets no special phase for someone
    // else's item any more.
    const viewedByTeammate = item({
      tier: 'T2',
      status: 'approved',
      requesting_member_id: 'someone-else',
    });
    expect(consentPhase(viewedByTeammate, 'me', false)).toBe('pending');
  });
});

describe('batchProgressLabel', () => {
  test('counts executed / approved / edited as approved, out of the whole batch', () => {
    const items = [
      item({ id: 'a', status: 'executed' }),
      item({ id: 'b', status: 'pending' }),
      item({ id: 'c', status: 'rejected' }),
      item({ id: 'd', status: 'pending' }),
      item({ id: 'e', status: 'pending' }),
    ];
    expect(batchProgressLabel(items)).toBe('1 of 5 approved');
  });
  test('a fresh batch with nothing resolved yet', () => {
    const items = [item({ id: 'a' }), item({ id: 'b' })];
    expect(batchProgressLabel(items)).toBe('0 of 2 approved');
  });
});

describe('pendingQueueRows — task 6: grouping is a display concern only', () => {
  test('ungrouped pending items each render as their own single row, in order', () => {
    const all = [item({ id: 'a', batch_id: null }), item({ id: 'b', batch_id: null })];
    const rows = pendingQueueRows(all, all);
    expect(rows).toEqual([
      { kind: 'single', item: all[0] },
      { kind: 'single', item: all[1] },
    ]);
  });

  test('items sharing a batch_id collapse into one batch row', () => {
    const batchId = 'batch-1';
    const a = item({ id: 'a', batch_id: batchId });
    const b = item({ id: 'b', batch_id: batchId });
    const c = item({ id: 'c', batch_id: null });
    const rows = pendingQueueRows([a, b, c], [a, b, c]);

    expect(rows).toEqual([
      { kind: 'batch', batchId, items: [a, b], totalCount: 2, progressLabel: '0 of 2 approved' },
      { kind: 'single', item: c },
    ]);
  });

  test('progress counts already-resolved siblings that no longer appear in the pending list', () => {
    const batchId = 'batch-1';
    const approved = item({ id: 'a', batch_id: batchId, status: 'executed' });
    const rejected = item({ id: 'b', batch_id: batchId, status: 'rejected' });
    const stillPending = item({ id: 'c', batch_id: batchId, status: 'pending' });
    // The inbox's `pending` list only ever holds still-pending items.
    const rows = pendingQueueRows([approved, rejected, stillPending], [stillPending]);

    expect(rows).toEqual([
      {
        kind: 'batch',
        batchId,
        items: [stillPending], // only the actionable one gets a card
        totalCount: 3,
        progressLabel: '1 of 3 approved',
      },
    ]);
  });

  test('a fully-resolved batch contributes no row once nothing is left pending', () => {
    const batchId = 'batch-1';
    const approved = item({ id: 'a', batch_id: batchId, status: 'executed' });
    const rejected = item({ id: 'b', batch_id: batchId, status: 'rejected' });
    expect(pendingQueueRows([approved, rejected], [])).toEqual([]);
  });

  test('two distinct batches each get their own row', () => {
    const a1 = item({ id: 'a1', batch_id: 'batch-1' });
    const a2 = item({ id: 'a2', batch_id: 'batch-1' });
    const b1 = item({ id: 'b1', batch_id: 'batch-2' });
    const rows = pendingQueueRows([a1, a2, b1], [a1, a2, b1]);

    expect(rows.map((r) => r.kind)).toEqual(['batch', 'batch']);
    expect(rows).toEqual([
      { kind: 'batch', batchId: 'batch-1', items: [a1, a2], totalCount: 2, progressLabel: '0 of 2 approved' },
      { kind: 'batch', batchId: 'batch-2', items: [b1], totalCount: 1, progressLabel: '0 of 1 approved' },
    ]);
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

describe('expiry', () => {
  const HOUR = 60 * 60 * 1000;
  const now = Date.parse('2026-08-31T12:00:00Z');
  const item = (over: Partial<ConsentItem> = {}): ConsentItem => ({
    id: 'c1',
    team_id: 't1',
    requesting_member_id: 'me',
    tool_name: 'task_create',
    tool_args: {},
    source_snippet: null,
    action_hash: 'abc',
    status: 'pending',
    reversible: true,
    tier: 'T1',
    expires_at: new Date(now + 24 * HOUR).toISOString(),
    created_at: new Date(now - HOUR).toISOString(),
    resolved_at: null,
    batch_id: null,
    ...over,
  });

  it('a live item inside its backstop is actionable', () => {
    expect(isExpired(item(), now)).toBe(false);
    expect(isActionable(item(), now)).toBe(true);
  });

  it('an item past its backstop is not actionable', () => {
    // The bug: status stays 'pending' forever because nothing sweeps the
    // queue, so without this the card offered an APPROVE button that
    // execute_consent refuses with a 409.
    const dead = item({ expires_at: new Date(now - HOUR).toISOString() });
    expect(isExpired(dead, now)).toBe(true);
    expect(isActionable(dead, now)).toBe(false);
  });

  it('treats the exact expiry instant as expired', () => {
    // execute_consent uses `expires_at < now()`; a card claiming one more
    // second of life than the executor allows is the wrong way to be wrong.
    const edge = item({ expires_at: new Date(now).toISOString() });
    expect(isExpired(edge, now)).toBe(true);
  });

  it('an item with no backstop never expires', () => {
    expect(isExpired(item({ expires_at: null }), now)).toBe(false);
  });

  it('reports an expired item as its own phase, not as pending', () => {
    const dead = item({ expires_at: new Date(now - HOUR).toISOString() });
    expect(consentPhase(dead, 'me', false, now)).toBe('expired');
  });

  it('an already-resolved item keeps its resolved phase', () => {
    const done = item({
      status: 'executed',
      expires_at: new Date(now - HOUR).toISOString(),
    });
    expect(consentPhase(done, 'me', false, now)).toBe('executed');
  });
});
