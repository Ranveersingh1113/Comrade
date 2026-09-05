import { useCallback, useEffect, useState } from 'react';
import { supabase } from '../lib/supabase';
import type { MemoryCompilation, Message } from '../lib/types';
import { useTeam } from '../state/TeamContext';
import { useTeamRealtime } from './useRealtime';

export interface MessagesState {
  messages: Message[];
  /** diff_message_id -> compilation, for rendering memory diff cards inline. */
  compilationsByMessage: Map<string, MemoryCompilation>;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}

export function useMessages(threadId: string): MessagesState {
  const { team } = useTeam();
  const teamId = team?.id ?? '';
  const [messages, setMessages] = useState<Message[]>([]);
  const [compilationsByMessage, setCompilations] = useState<Map<string, MemoryCompilation>>(
    new Map(),
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!teamId) return;
    let q = supabase
      .from('messages')
      .select('*')
      .eq('team_id', teamId)
      .order('created_at')
      .limit(500);
    q = q.eq('thread_id', threadId);
    const { data, error: err } = await q;
    if (err) {
      setError(err.message);
      setLoading(false);
      return;
    }
    setMessages((data as Message[] | null) ?? []);
    setError(null);

    const { data: comps } = await supabase
      .from('memory_compilations')
      .select('*')
      .eq('team_id', teamId)
      .not('diff_message_id', 'is', null);
    const map = new Map<string, MemoryCompilation>();
    for (const c of (comps as MemoryCompilation[] | null) ?? []) {
      if (c.diff_message_id) map.set(c.diff_message_id, c);
    }
    setCompilations(map);
    setLoading(false);
  }, [teamId, threadId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  useTeamRealtime('messages', teamId, refresh);

  return { messages, compilationsByMessage, loading, error, refresh };
}
