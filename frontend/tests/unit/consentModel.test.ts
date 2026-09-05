import { describe, expect, it, test } from 'vitest';
import {
  argsPretty, consentPhase, isActionable, isExpired,
} from '../../src/lib/consentModel';
import type { ConsentItem } from '../../src/lib/types';

const item = (over: Partial<ConsentItem>): ConsentItem => ({
  id: 'c1', team_id: 't1', requesting_member_id: 'u1', tool_name: 'task_create',
  tool_args: { body: 'hi' }, source_snippet: null, action_hash: 'h', status: 'pending',
  reversible: true, expires_at: null, created_at: '1', resolved_at: null,
  tier: 'T2', thread_id: 'thread-1', agent_run_id: null, resolution_reason: null,
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
    thread_id: 'thread-1',
    agent_run_id: null,
    resolution_reason: null,
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
