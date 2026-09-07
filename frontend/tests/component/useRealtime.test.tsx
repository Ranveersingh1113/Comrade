import { beforeEach, describe, expect, test, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';

/**
 * 🔴 THE DEFECTS.
 *
 * Every postgres_changes event called `onChange()` straight away, and the
 * callers' `onChange` is a full refetch. Ten messages arriving together — one
 * agent turn writing its reply while two people type — meant ten complete
 * reloads of the thread, each one throwing away the answer the one before it
 * had just fetched.
 *
 * And the subscription's status was ignored entirely. A websocket that dropped
 * took every event with it until the member happened to focus the window;
 * there was no refetch on RESUBSCRIBE, so a reconnect restored the pipe
 * without restoring anything that had come down it, and nothing anywhere told
 * the member their room had gone quiet for a reason.
 */

interface Handler { (): void }

const { hub } = vi.hoisted(() => ({
  hub: {
    fire: null as null | Handler,
    status: null as null | ((s: string) => void),
    removed: 0,
  },
}));

vi.mock('../../src/lib/supabase', () => ({
  supabase: {
    channel: () => {
      const ch = {
        on: (_event: string, _opts: unknown, cb: Handler) => {
          hub.fire = cb;
          return ch;
        },
        subscribe: (cb?: (s: string) => void) => {
          hub.status = cb ?? null;
          cb?.('SUBSCRIBED');
          return ch;
        },
      };
      return ch;
    },
    removeChannel: () => {
      hub.removed += 1;
      return Promise.resolve('ok');
    },
  },
}));

import { useTeamRealtime } from '../../src/hooks/useRealtime';

beforeEach(() => {
  hub.fire = null;
  hub.status = null;
  hub.removed = 0;
  vi.useRealTimers();
});

describe('useTeamRealtime', () => {
  test('a burst of events causes one refetch, not one per event', async () => {
    const calls: number[] = [];
    renderHook(() => useTeamRealtime('messages', 'team-1', () => calls.push(1)));

    act(() => {
      for (let i = 0; i < 10; i += 1) hub.fire?.();
    });

    await waitFor(() => expect(calls.length).toBeGreaterThan(0));
    // Give any stragglers a chance to land before asserting the count.
    await new Promise((r) => setTimeout(r, 120));
    expect(calls.length).toBe(1);
  });

  test('a later event after the burst still refetches', async () => {
    // Coalescing must not become swallowing: the window closes.
    const calls: number[] = [];
    renderHook(() => useTeamRealtime('messages', 'team-1', () => calls.push(1)));

    act(() => { hub.fire?.(); });
    await waitFor(() => expect(calls.length).toBe(1));
    act(() => { hub.fire?.(); });

    await waitFor(() => expect(calls.length).toBe(2));
  });

  test('reconnecting refetches what was missed while the socket was down', async () => {
    const calls: number[] = [];
    const { result } = renderHook(() =>
      useTeamRealtime('messages', 'team-1', () => calls.push(1)));
    const before = calls.length;

    act(() => { hub.status?.('CHANNEL_ERROR'); });
    expect(result.current.connected).toBe(false);
    act(() => { hub.status?.('SUBSCRIBED'); });

    await waitFor(() => expect(calls.length).toBeGreaterThan(before));
    expect(result.current.connected).toBe(true);
  });

  test('the first subscribe does not trigger a refetch on its own', async () => {
    // The caller has just loaded; refetching immediately is a duplicate.
    const calls: number[] = [];
    renderHook(() => useTeamRealtime('messages', 'team-1', () => calls.push(1)));

    await new Promise((r) => setTimeout(r, 120));

    expect(calls).toEqual([]);
  });

  test('a dropped connection is reported so the room can say so', () => {
    const { result } = renderHook(() =>
      useTeamRealtime('messages', 'team-1', () => {}));

    expect(result.current.connected).toBe(true);
    act(() => { hub.status?.('TIMED_OUT'); });

    expect(result.current.connected).toBe(false);
  });
});
