import { describe, expect, test } from 'vitest';
import {
  confirmPatch, nextStatus, taskAffordance, taskMark, taskPill,
} from '../../src/lib/taskFlow';

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
