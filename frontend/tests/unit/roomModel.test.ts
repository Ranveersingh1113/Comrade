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
    expect(
      classifyMessage(msg({ sender_kind: 'ai', sender_id: null }), new Set()).kind,
    ).toBe('ai');
  });
  test('diff card = AI message pointed at by a compilation', () => {
    const c = classifyMessage(msg({ sender_kind: 'ai', sender_id: null }), new Set(['m1']));
    expect(c).toEqual({ kind: 'ai', isDiffCard: true });
  });
  test('a USER message id in diffCardIds is not a diff card', () => {
    expect(classifyMessage(msg({}), new Set(['m1'])).isDiffCard).toBe(false);
  });
  test('deleted-for-me only does not tombstone for everyone', () => {
    expect(classifyMessage(msg({ deleted_scope: 'me' }), new Set()).kind).toBe('user');
  });
});

describe('memberBars', () => {
  const prof = (id: string, name: string) =>
    ({ profile: { id, display_name: name } as Profile });
  const task = (id: string, assignee: string | null, status: Task['status']): Task =>
    ({ id, assignee_id: assignee, status }) as Task;

  test('groups tasks per member and counts done', () => {
    const bars = memberBars(
      [prof('u1', 'Priya Sharma'), prof('u2', 'Marcus Lee')],
      [task('t1', 'u1', 'done'), task('t2', 'u1', 'proposed'), task('t3', 'u2', 'in_progress')],
    );
    expect(bars).toHaveLength(2);
    expect(bars[0]).toMatchObject({ userId: 'u1', name: 'Priya Sharma', doneCount: 1 });
    expect(bars[0].tasks.map((t) => t.id)).toEqual(['t1', 't2']);
    expect(bars[1].doneCount).toBe(0);
  });
  test("unassigned tasks appear in nobody's bar; empty roster -> []", () => {
    expect(memberBars([], [task('t1', null, 'proposed')])).toEqual([]);
  });
});
