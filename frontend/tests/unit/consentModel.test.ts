import { describe, expect, test } from 'vitest';
import { argsPretty, consentPhase } from '../../src/lib/consentModel';
import type { ConsentItem } from '../../src/lib/types';

const item = (over: Partial<ConsentItem>): ConsentItem => ({
  id: 'c1', team_id: 't1', requesting_member_id: 'u1', tool_name: 'post_group_message',
  tool_args: { body: 'hi' }, source_snippet: null, action_hash: 'h', status: 'pending',
  reversible: true, expires_at: null, created_at: '1', resolved_at: null,
  tier: 'T2',
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
