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
  test('T3 APPROVED but uncountersigned still lets a teammate countersign', () => {
    // regression: the awaiting-branch must not shadow the teammate's view
    expect(consentPhase(item({ tier: 'T3', status: 'approved' }), 'u2', false))
      .toBe('can_countersign');
  });
  test('T3 countersigned but unapproved stays ACTIONABLE for the requester', () => {
    // Their key still has to turn — hiding the approve buttons would deadlock.
    expect(consentPhase(item({ tier: 'T3', second_approver_id: 'u2' }), 'u1', false))
      .toBe('pending');
  });
  test('teammate who countersigned sees countersigned_pending, not can_countersign', () => {
    expect(consentPhase(item({ tier: 'T3', second_approver_id: 'u2' }), 'u2', false))
      .toBe('countersigned_pending');
  });
  test('executed / rejected / cancelled pass through', () => {
    expect(consentPhase(item({ status: 'executed' }), 'u1', false)).toBe('executed');
    expect(consentPhase(item({ status: 'rejected' }), 'u1', false)).toBe('rejected');
    expect(consentPhase(item({ status: 'cancelled' }), 'u1', false)).toBe('cancelled');
  });
  test('T2 never asks for a countersign, even from a teammate viewer', () => {
    expect(consentPhase(item({}), 'u2', false)).toBe('pending');
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
