import { useCallback, useEffect, useMemo, useState } from 'react';
import { supabase } from '../lib/supabase';
import type { Thread, ThreadVisibility } from '../lib/types';
import { useTeam } from '../state/TeamContext';
import { useTeamRealtime } from './useRealtime';

export interface ThreadParticipant {
  thread_id: string;
  team_id: string;
  user_id: string;
  added_by: string;
  joined_at: string;
}

/** How a thread should be described to somebody looking at a list of them. */
export type ThreadAudience = 'Team' | 'Selected members' | 'Private';

export function audienceOf(thread: Thread, participants: number): ThreadAudience {
  if (thread.visibility === 'team') return 'Team';
  // 🔴 Everything restricted was labelled "Selected members", including a
  // thread with exactly one member in it. "Selected members" describes a small
  // group; a thread nobody else is in is Private, and the difference is the
  // whole question a member is asking when they scan this list.
  return participants > 1 ? 'Selected members' : 'Private';
}

/** The member-visible thread list, its rosters, and the creation paths. */
export function useThreads() {
  const { team, myUserId } = useTeam();
  const [threads, setThreads] = useState<Thread[]>([]);
  const [participants, setParticipants] = useState<ThreadParticipant[]>([]);
  const [error, setError] = useState<string | null>(null);
  const teamId = team?.id ?? '';

  const load = useCallback(async () => {
    if (!teamId) return;
    const { data, error: queryError } = await supabase
      .from('threads').select('*').eq('team_id', teamId).is('archived_at', null)
      .order('updated_at', { ascending: false });
    // Deliberately no `setThreads([])` on failure: showing an empty list
    // because one request did not arrive tells a member their threads are
    // gone. Keep what is on screen and say the refresh failed.
    if (queryError) setError(queryError.message);
    else { setThreads((data as Thread[] | null) ?? []); setError(null); }

    const roster = await supabase
      .from('thread_participants').select('*').eq('team_id', teamId);
    if (!roster.error) {
      setParticipants((roster.data as ThreadParticipant[] | null) ?? []);
    }
  }, [teamId]);
  useEffect(() => { void load(); }, [load]);

  // 🔴 Neither table was watched, so a teammate creating a thread, renaming
  // one, or adding somebody to one was invisible until a reload.
  useTeamRealtime('threads', teamId, load);
  useTeamRealtime('thread_participants', teamId, load);

  const counts = useMemo(() => {
    const byThread = new Map<string, number>();
    for (const p of participants) {
      byThread.set(p.thread_id, (byThread.get(p.thread_id) ?? 0) + 1);
    }
    return byThread;
  }, [participants]);

  const participantsOf = useCallback(
    (threadId: string) => participants.filter((p) => p.thread_id === threadId),
    [participants],
  );

  const createThread = useCallback(async (
    { visibility = 'team', members = [] }:
    { visibility?: ThreadVisibility; members?: string[] } = {},
  ): Promise<Thread> => {
    if (!teamId) throw new Error('No team selected');
    const thread: Thread = {
      id: crypto.randomUUID(), team_id: teamId, title: 'New thread', visibility,
      kind: 'discussion', work_state: null, owner_id: null, created_by: myUserId,
      created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
    };
    const { error: insertError } = await supabase.from('threads').insert(thread);
    if (insertError) throw new Error(insertError.message);
    if (visibility === 'restricted') {
      // The creator first: RLS lets only the thread's creator add anyone, and
      // a restricted thread with nobody in it is invisible even to its author.
      const rows = [myUserId, ...members.filter((id) => id !== myUserId)]
        .map((user_id) => ({
          thread_id: thread.id, team_id: teamId, user_id, added_by: myUserId,
        }));
      const { error: rosterError } = await supabase
        .from('thread_participants').insert(rows);
      if (rosterError) throw new Error(rosterError.message);
    }
    setThreads((current) => [thread, ...current]);
    return thread;
  }, [myUserId, teamId]);

  const createPublicThread = useCallback(() => createThread(), [createThread]);

  return {
    threads, error, load, participants, counts, participantsOf,
    createThread, createPublicThread,
  };
}
