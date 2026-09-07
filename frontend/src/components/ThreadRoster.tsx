import { useCallback, useEffect, useState } from 'react';
import { supabase } from '../lib/supabase';
import { useTeamRealtime } from '../hooks/useRealtime';
import { useTeam } from '../state/TeamContext';
import type { Thread } from '../lib/types';

interface Participant {
  thread_id: string;
  user_id: string;
  added_by: string;
  joined_at: string;
}

interface RosterEvent {
  id: string;
  user_id: string;
  actor_id: string | null;
  action: 'added' | 'removed';
  at: string;
}

/**
 * Who is in a restricted thread, and who put them there.
 *
 * 🔴 There was no way to see or change this from the product at all. A
 * restricted thread's membership was set when it was created and never again,
 * and nothing showed the people in it who else could read what they wrote.
 */
export function ThreadRoster({ thread }: { thread: Thread }) {
  const { team, roster, myUserId, profileOf } = useTeam();
  const teamId = team?.id ?? '';
  const [people, setPeople] = useState<Participant[]>([]);
  const [events, setEvents] = useState<RosterEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  const load = useCallback(async () => {
    if (!teamId) return;
    const [current, history] = await Promise.all([
      supabase.from('thread_participants').select('*')
        .eq('team_id', teamId).eq('thread_id', thread.id),
      supabase.from('thread_participant_events').select('*')
        .eq('team_id', teamId).eq('thread_id', thread.id)
        .order('at', { ascending: false }).limit(20),
    ]);
    // Keep what is on screen if a refresh fails: an empty roster would read as
    // "everyone left".
    if (!current.error) setPeople((current.data as Participant[] | null) ?? []);
    if (!history.error) setEvents((history.data as RosterEvent[] | null) ?? []);
  }, [teamId, thread.id]);

  useEffect(() => { void load(); }, [load]);
  useTeamRealtime('thread_participants', teamId, load, `thread_id=eq.${thread.id}`);

  const isCreator = thread.created_by === myUserId;
  const inThread = new Set(people.map((p) => p.user_id));
  const addable = roster.filter((m) => !inThread.has(m.membership.user_id));

  const add = async (userId: string) => {
    setPending(userId);
    setError(null);
    const { error: err } = await supabase.from('thread_participants').insert({
      thread_id: thread.id, team_id: teamId, user_id: userId, added_by: myUserId,
    });
    setPending(null);
    if (err) setError(err.message);
    else await load();
  };

  const remove = async (userId: string) => {
    setPending(userId);
    setError(null);
    const { error: err } = await supabase.from('thread_participants').delete()
      .eq('thread_id', thread.id).eq('user_id', userId);
    setPending(null);
    if (err) setError(err.message);
    else await load();
  };

  const name = (userId: string | null) =>
    (userId ? profileOf(userId)?.display_name ?? 'Someone' : 'Someone');

  return (
    <section data-thread-roster style={{ display: 'grid', gap: 8 }}>
      <div className="micro-label">In this thread ({people.length})</div>
      <div style={{ display: 'grid', gap: 4, fontSize: 12.5 }}>
        {people.map((p) => (
          <div key={p.user_id} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span>{name(p.user_id)}</span>
            {thread.created_by === p.user_id && (
              <span className="mono" style={{ fontSize: 9, color: 'var(--faint)' }}>OWNER</span>
            )}
            {(isCreator || p.user_id === myUserId) && thread.created_by !== p.user_id && (
              <button
                className="btn-ghost"
                style={{ marginLeft: 'auto', fontSize: 11 }}
                disabled={pending === p.user_id}
                onClick={() => void remove(p.user_id)}
              >
                {p.user_id === myUserId ? 'Leave' : 'Remove'}
              </button>
            )}
          </div>
        ))}
      </div>

      {isCreator && addable.length > 0 && (
        <>
          <button className="btn-ghost" style={{ fontSize: 11, justifySelf: 'start' }}
            onClick={() => setOpen((v) => !v)}>
            {open ? 'Cancel' : 'Add someone'}
          </button>
          {open && (
            <div style={{ display: 'grid', gap: 6 }}>
              {/* Said before the click, not after. Adding somebody to a thread
                  does not start their view at today: they get everything that
                  has ever been said in it, and the person doing the adding is
                  usually thinking about the next message rather than the last
                  three months of them. */}
              <p data-history-warning style={{ fontSize: 12, color: 'var(--muted)', margin: 0 }}>
                Anyone you add can read this thread’s <b>entire history</b> —
                every message, file and Comrade run in it, from the beginning.
              </p>
              {addable.map((m) => (
                <button
                  key={m.membership.user_id}
                  className="btn-secondary"
                  style={{ fontSize: 12, justifySelf: 'start' }}
                  disabled={pending === m.membership.user_id}
                  onClick={() => void add(m.membership.user_id)}
                >
                  Add {m.profile.display_name}
                </button>
              ))}
            </div>
          )}
        </>
      )}

      {error && <p style={{ color: 'var(--terracotta)', fontSize: 12 }}>{error}</p>}

      {events.length > 0 && (
        <details>
          <summary className="micro-label" style={{ cursor: 'pointer' }}>Who changed this</summary>
          <div style={{ display: 'grid', gap: 3, fontSize: 11.5, color: 'var(--muted)', marginTop: 6 }}>
            {events.map((e) => (
              <span key={e.id}>
                {name(e.actor_id)} {e.action} {name(e.user_id)} ·{' '}
                {new Date(e.at).toLocaleDateString()}
              </span>
            ))}
          </div>
        </details>
      )}
    </section>
  );
}
