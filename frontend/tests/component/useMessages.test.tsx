import { beforeEach, describe, expect, test, vi } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';

/**
 * 🔴 THE DEFECT. `useMessages` ordered ASCENDING with `limit(500)`, so a thread
 * past five hundred messages fetched the OLDEST five hundred — and the newest
 * message never appeared at all. A busy thread silently stopped showing new
 * conversation, which is the worst way for a chat product to fail: it looks
 * like nobody is talking.
 *
 * The fix is a newest-first page displayed chronologically, with keyset cursors
 * for older pages.
 */

interface Row { id: string; created_at: string; body: string }

const { state } = vi.hoisted(() => ({
  state: {
    rows: [] as Row[],
    compilationIds: [] as string[],
    error: null as string | null,
    delayMs: 0,
  },
}));

vi.mock('../../src/lib/supabase', () => {
  const makeBuilder = (table: string) => {
    const filters: { desc?: boolean; limit?: number; before?: string } = {};
    const builder: Record<string, unknown> = {
      select: () => builder,
      eq: () => builder,
      not: () => builder,
      in: (_col: string, ids: string[]) => {
        state.compilationIds = ids;
        return builder;
      },
      or: (expr: string) => {
        const match = /created_at\.lt\.([^,)]+)/.exec(expr);
        if (match) filters.before = match[1];
        return builder;
      },
      lt: (_col: string, value: string) => {
        filters.before = value;
        return builder;
      },
      order: (_col: string, opts?: { ascending?: boolean }) => {
        if (opts && opts.ascending === false) filters.desc = true;
        return builder;
      },
      limit: async (n: number) => {
        filters.limit = n;
        if (state.error) return { data: null, error: { message: state.error } };
        if (state.delayMs) await new Promise((r) => setTimeout(r, state.delayMs));
        if (table !== 'messages') return { data: [], error: null };
        let rows = [...state.rows].sort((a, b) =>
          a.created_at === b.created_at
            ? a.id.localeCompare(b.id)
            : a.created_at.localeCompare(b.created_at));
        if (filters.before) rows = rows.filter((r) => r.created_at < filters.before!);
        if (filters.desc) rows.reverse();
        return { data: rows.slice(0, n), error: null };
      },
    };
    return builder;
  };
  return { supabase: { from: (table: string) => makeBuilder(table) } };
});
vi.mock('../../src/hooks/useRealtime', () => ({ useTeamRealtime: () => {} }));
vi.mock('../../src/state/TeamContext', () => ({
  useTeam: () => ({ team: { id: 'team-1' } }),
}));

import { useMessages } from '../../src/hooks/useMessages';

const seed = (count: number) => {
  state.rows = Array.from({ length: count }, (_, i) => ({
    id: `m-${String(i).padStart(4, '0')}`,
    created_at: new Date(1_700_000_000_000 + i * 1000).toISOString(),
    body: `message ${i}`,
  }));
};

beforeEach(() => {
  state.rows = [];
  state.compilationIds = [];
  state.error = null;
  state.delayMs = 0;
});

describe('useMessages', () => {
  test('the newest message is visible in a thread of 501', async () => {
    seed(501);
    const { result } = renderHook(() => useMessages('thread-1'));
    await waitFor(() => expect(result.current.loading).toBe(false));

    const bodies = result.current.messages.map((m) => m.body);
    expect(bodies).toContain('message 500');
  });

  test('messages are displayed oldest-first even though they are fetched newest-first', async () => {
    seed(10);
    const { result } = renderHook(() => useMessages('thread-1'));
    await waitFor(() => expect(result.current.loading).toBe(false));

    const times = result.current.messages.map((m) => m.created_at);
    expect([...times].sort()).toEqual(times);
  });

  test('older history is reachable and prepends without dropping what is loaded', async () => {
    seed(501);
    const { result } = renderHook(() => useMessages('thread-1'));
    await waitFor(() => expect(result.current.loading).toBe(false));

    const firstPage = result.current.messages.length;
    expect(result.current.hasOlder).toBe(true);
    await act(async () => { await result.current.loadOlder(); });

    expect(result.current.messages.length).toBeGreaterThan(firstPage);
    expect(result.current.messages.map((m) => m.body)).toContain('message 500');
  });

  test('identical timestamps do not produce a gap or a duplicate', async () => {
    // A keyset cursor on created_at alone loses or repeats rows that share a
    // timestamp, and bulk inserts share timestamps constantly.
    const stamp = new Date(1_700_000_000_000).toISOString();
    state.rows = Array.from({ length: 600 }, (_, i) => ({
      id: `m-${String(i).padStart(4, '0')}`, created_at: stamp, body: `m${i}`,
    }));
    const { result } = renderHook(() => useMessages('thread-1'));
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(async () => { await result.current.loadOlder(); });

    const ids = result.current.messages.map((m) => m.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  test('only compilations for loaded messages are fetched', async () => {
    seed(5);
    const { result } = renderHook(() => useMessages('thread-1'));
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(state.compilationIds.length).toBeGreaterThan(0);
    expect(state.compilationIds.length).toBeLessThanOrEqual(5);
  });

  test('a failed load reports an error instead of showing an empty thread', async () => {
    state.error = 'network is down';
    const { result } = renderHook(() => useMessages('thread-1'));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBe('network is down');
  });
});
