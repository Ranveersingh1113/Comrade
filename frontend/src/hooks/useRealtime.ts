import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { supabase } from '../lib/supabase';

/** How long events are gathered before one refetch runs.
 *
 *  🔴 There was no window. Every postgres_changes event called `onChange`
 *  immediately, and every caller's `onChange` is a FULL refetch — ten messages
 *  arriving together meant ten complete reloads of the thread, each throwing
 *  away the answer the one before it had just fetched. Short enough that a
 *  single message still feels instant; long enough that a burst is one read. */
const COALESCE_MS = 80;

export interface RealtimeState {
  /** False while the socket is down, so a room can say so rather than
   *  quietly showing history that has stopped updating. */
  connected: boolean;
}

/**
 * Subscribe to postgres_changes for one table scoped to a team, and call
 * `onChange` when something changes — coalesced, not once per event.
 *
 * Each caller gets its own uniquely-named channel: supabase-js returns the
 * existing channel object for a repeated name, and adding an `.on()` handler
 * to an already-subscribed channel throws. Two components watching the same
 * table must not collide.
 *
 * Tables are published server-side by migration
 * 20260719120000_realtime_publication.sql.
 */
export function useTeamRealtime(
  // Only tables in the supabase_realtime publication belong here — a
  // subscription to one that is absent compiles and delivers nothing.
  table: 'messages' | 'tasks' | 'consent_queue' | 'sandbox_processes',
  teamId: string,
  onChange: () => void,
  filter = `team_id=eq.${teamId}`,
): RealtimeState {
  const cbRef = useRef(onChange);
  cbRef.current = onChange;
  const instanceId = useId();
  const [connected, setConnected] = useState(true);

  // Trailing-edge coalescing. The first event opens a window and every event
  // inside it collapses into the same refetch.
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const schedule = useCallback(() => {
    if (timer.current) return;
    timer.current = setTimeout(() => {
      timer.current = null;
      cbRef.current();
    }, COALESCE_MS);
  }, []);

  useEffect(() => {
    // Refetch on focus regardless: a tab that was asleep may have missed
    // events the socket never redelivered.
    const onFocus = () => schedule();
    window.addEventListener('focus', onFocus);

    if (!teamId) {
      return () => window.removeEventListener('focus', onFocus);
    }

    // Whether we have been up before. The FIRST subscribe must not refetch —
    // the caller has just loaded — but every one after it must, because a
    // reconnect restores the pipe and nothing that came down it while it was
    // broken. Without this, a dropped websocket silently froze a room until
    // somebody happened to focus the window.
    let wasConnected = false;

    const channel = supabase
      .channel(`rt:${table}:${filter}:${instanceId}`)
      .on(
        'postgres_changes',
        { event: '*', schema: 'public', table, filter },
        () => schedule(),
      )
      .subscribe((status: string) => {
        if (status === 'SUBSCRIBED') {
          setConnected(true);
          if (wasConnected) schedule();
          wasConnected = true;
          return;
        }
        if (status === 'CHANNEL_ERROR' || status === 'TIMED_OUT' || status === 'CLOSED') {
          setConnected(false);
        }
      });

    return () => {
      window.removeEventListener('focus', onFocus);
      if (timer.current) {
        clearTimeout(timer.current);
        timer.current = null;
      }
      void supabase.removeChannel(channel);
    };
  }, [table, teamId, filter, instanceId, schedule]);

  return { connected };
}
