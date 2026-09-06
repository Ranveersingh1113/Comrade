import { useCallback, useEffect, useState } from 'react';
import { supabase } from '../lib/supabase';
import type { Thread } from '../lib/types';
import { useTeam } from '../state/TeamContext';

/** The member-visible thread list and the one public creation path. */
export function useThreads() {
  const { team, myUserId } = useTeam();
  const [threads, setThreads] = useState<Thread[]>([]);
  const [error, setError] = useState<string | null>(null);
  const teamId = team?.id ?? '';

  const load = useCallback(async () => {
    if (!teamId) return;
    const { data, error: queryError } = await supabase
      .from('threads').select('*').eq('team_id', teamId).is('archived_at', null)
      .order('updated_at', { ascending: false });
    if (queryError) setError(queryError.message);
    else { setThreads((data as Thread[] | null) ?? []); setError(null); }
  }, [teamId]);
  useEffect(() => { void load(); }, [load]);

  const createPublicThread = useCallback(async (): Promise<Thread> => {
    if (!teamId) throw new Error('No team selected');
    const thread: Thread = {
      id: crypto.randomUUID(), team_id: teamId, title: 'New thread', visibility: 'team',
      kind: 'discussion', work_state: null, owner_id: null, created_by: myUserId,
      created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
    };
    const { error: insertError } = await supabase.from('threads').insert(thread);
    if (insertError) throw new Error(insertError.message);
    setThreads((current) => [thread, ...current]);
    return thread;
  }, [myUserId, teamId]);

  return { threads, error, load, createPublicThread };
}
