import { useEffect, useId, useRef } from 'react';
import { supabase } from '../lib/supabase';

/**
 * Subscribe to postgres_changes for one table scoped to a team, and call
 * `onChange` on any insert/update/delete. RLS scopes what events arrive.
 *
 * Each caller gets its own uniquely-named channel: supabase-js returns the
 * existing channel object for a repeated name, and adding an `.on()` handler
 * to an already-subscribed channel throws. Two components watching the same
 * table must not collide.
 *
 * Tables are published server-side by migration
 * 20260719120000_realtime_publication.sql; the focus-refetch remains as a
 * belt-and-braces fallback for dropped websocket connections.
 */
export function useTeamRealtime(
  table: 'messages' | 'tasks' | 'consent_queue',
  teamId: string,
  onChange: () => void,
  filter = `team_id=eq.${teamId}`,
): void {
  const cbRef = useRef(onChange);
  cbRef.current = onChange;
  const instanceId = useId();

  useEffect(() => {
    // Refetch on focus regardless — this is the fallback while the tables are
    // not yet in the realtime publication.
    const onFocus = () => cbRef.current();
    window.addEventListener('focus', onFocus);

    if (!teamId) {
      return () => window.removeEventListener('focus', onFocus);
    }

    const channel = supabase
      .channel(`rt:${table}:${filter}:${instanceId}`)
      .on(
        'postgres_changes',
        { event: '*', schema: 'public', table, filter },
        () => cbRef.current(),
      )
      .subscribe();

    return () => {
      window.removeEventListener('focus', onFocus);
      void supabase.removeChannel(channel);
    };
  }, [table, teamId, filter, instanceId]);
}
