import { useCallback, useEffect, useRef, useState } from 'react';
import { supabase } from '../lib/supabase';
import type { MemoryCompilation, Message } from '../lib/types';
import { useTeam } from '../state/TeamContext';
import { useTeamRealtime } from './useRealtime';

/**
 * 🔴 THE DEFECT THIS REPLACES. The query was `.order('created_at').limit(500)`
 * — ascending — so a thread past five hundred messages fetched the OLDEST five
 * hundred and the newest message never appeared. A busy thread silently stopped
 * showing new conversation, which is the worst way for a chat product to fail:
 * it looks like nobody is talking rather than like something is broken.
 *
 * Newest page first, displayed chronologically, with keyset cursors for older
 * pages.
 */
export interface MessagesState {
  messages: Message[];
  /** False while realtime is down: the thread is showing history that has
   *  stopped updating, and saying so beats looking idle. */
  live: boolean;
  /** diff_message_id -> compilation, for rendering memory diff cards inline. */
  compilationsByMessage: Map<string, MemoryCompilation>;
  loading: boolean;
  loadingOlder: boolean;
  hasOlder: boolean;
  error: string | null;
  loadOlder: () => Promise<void>;
  refresh: () => Promise<void>;
}

export const PAGE_SIZE = 100;

/** Merge by id, keeping chronological order.
 *
 *  By ID rather than by position: a refresh overlaps the newest page with what
 *  is already loaded, and appending blindly would show every recent message
 *  twice. Two messages can share a `created_at` — bulk inserts do it
 *  constantly — so the id is the tiebreak everywhere, in the sort and in the
 *  cursor. */
function merge(existing: Message[], incoming: Message[]): Message[] {
  const byId = new Map(existing.map((m) => [m.id, m]));
  for (const message of incoming) byId.set(message.id, message);
  return [...byId.values()].sort((a, b) =>
    a.created_at === b.created_at
      ? a.id.localeCompare(b.id)
      : a.created_at.localeCompare(b.created_at));
}

export function useMessages(threadId: string): MessagesState {
  const { team } = useTeam();
  const teamId = team?.id ?? '';
  const [messages, setMessages] = useState<Message[]>([]);
  const [compilationsByMessage, setCompilations] = useState<Map<string, MemoryCompilation>>(
    new Map(),
  );
  const [loading, setLoading] = useState(true);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [hasOlder, setHasOlder] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Which request is current. Switching threads quickly leaves an earlier
  // fetch in flight, and without this its response lands in the new thread —
  // one conversation's history rendered under another's title.
  const requestRef = useRef(0);

  const loadCompilations = useCallback(async (loaded: Message[], token: number) => {
    const ids = loaded.map((m) => m.id);
    if (!ids.length) {
      if (requestRef.current === token) setCompilations(new Map());
      return;
    }
    // Only the ones referenced by what is loaded. This used to fetch every
    // compilation the team had ever made, on every refresh, to render a
    // handful of cards.
    const { data, error: err } = await supabase
      .from('memory_compilations')
      .select('*')
      .eq('team_id', teamId)
      .in('diff_message_id', ids)
      .limit(PAGE_SIZE);
    if (requestRef.current !== token) return;
    if (err) {
      setError(err.message);
      return;
    }
    const map = new Map<string, MemoryCompilation>();
    for (const c of (data as MemoryCompilation[] | null) ?? []) {
      if (c.diff_message_id) map.set(c.diff_message_id, c);
    }
    setCompilations(map);
  }, [teamId]);

  const refresh = useCallback(async () => {
    if (!teamId) return;
    const token = ++requestRef.current;
    // DESCENDING, then reversed for display. Ascending with a limit returns
    // the beginning of the thread, which is the one page nobody is looking at.
    const { data, error: err } = await supabase
      .from('messages')
      .select('*')
      .eq('team_id', teamId)
      .eq('thread_id', threadId)
      .order('created_at', { ascending: false })
      .order('id', { ascending: false })
      .limit(PAGE_SIZE);
    if (requestRef.current !== token) return;
    if (err) {
      setError(err.message);
      setLoading(false);
      return;
    }
    const page = ((data as Message[] | null) ?? []).slice().reverse();
    setHasOlder(page.length >= PAGE_SIZE);
    setError(null);
    // Merged, not replaced: a refresh must not discard older pages someone
    // scrolled up to read.
    setMessages((current) => merge(current, page));
    setLoading(false);
    await loadCompilations(page, token);
  }, [teamId, threadId, loadCompilations]);

  const loadOlder = useCallback(async () => {
    if (!teamId || loadingOlder || !messages.length) return;
    setLoadingOlder(true);
    const token = requestRef.current;
    const oldest = messages[0];
    // Keyset, not offset: an offset shifts under every message that arrives
    // while someone is reading, which shows a duplicate or skips a line. The
    // id is in the cursor because timestamps are not unique.
    const { data, error: err } = await supabase
      .from('messages')
      .select('*')
      .eq('team_id', teamId)
      .eq('thread_id', threadId)
      .or(`created_at.lt.${oldest.created_at},and(created_at.eq.${oldest.created_at},id.lt.${oldest.id})`)
      .order('created_at', { ascending: false })
      .order('id', { ascending: false })
      .limit(PAGE_SIZE);
    if (requestRef.current !== token) {
      setLoadingOlder(false);
      return;
    }
    if (err) {
      setError(err.message);
      setLoadingOlder(false);
      return;
    }
    const older = ((data as Message[] | null) ?? []).slice().reverse();
    setHasOlder(older.length >= PAGE_SIZE);
    setMessages((current) => merge(current, older));
    setLoadingOlder(false);
    await loadCompilations(older, token);
  }, [teamId, threadId, messages, loadingOlder, loadCompilations]);

  useEffect(() => {
    // A new thread starts empty rather than showing the previous one's
    // history while its own first page is in flight.
    setMessages([]);
    setCompilations(new Map());
    setHasOlder(false);
    setLoading(true);
    void refresh();
  }, [refresh]);
  const { connected } = useTeamRealtime(
    'messages', teamId, refresh, `thread_id=eq.${threadId}`,
  );

  return {
    messages, compilationsByMessage, loading, loadingOlder, hasOlder,
    error, loadOlder, refresh, live: connected,
  };
}
