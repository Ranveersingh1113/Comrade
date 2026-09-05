import { useCallback, useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { supabase } from '../lib/supabase';
import type { Thread, ThreadKind, ThreadVisibility } from '../lib/types';
import { useTeam } from '../state/TeamContext';
import { GroupRoom } from './GroupRoom';

export function Threads() {
  const { threadId } = useParams<{ threadId: string }>();
  const { team, roster, myUserId } = useTeam();
  const [threads, setThreads] = useState<Thread[]>([]);
  const [creating, setCreating] = useState(false);
  const [title, setTitle] = useState('');
  const [visibility, setVisibility] = useState<ThreadVisibility>('team');
  const [kind, setKind] = useState<ThreadKind>('discussion');
  const [participants, setParticipants] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const teamId = team?.id ?? '';

  const load = useCallback(async () => {
    if (!teamId) return;
    const { data, error: queryError } = await supabase
      .from('threads').select('*').eq('team_id', teamId).is('archived_at', null).order('updated_at', { ascending: false });
    if (queryError) setError(queryError.message);
    else setThreads((data as Thread[] | null) ?? []);
  }, [teamId]);
  useEffect(() => { void load(); }, [load]);

  const create = async () => {
    const cleanTitle = title.trim();
    if (!cleanTitle || !teamId) return;
    const id = crypto.randomUUID();
    const thread = {
      id, team_id: teamId, title: cleanTitle, visibility, kind, created_by: myUserId,
      ...(visibility === 'restricted' ? { owner_id: myUserId } : {}),
      ...(kind === 'work' ? { work_state: 'planned' } : {}),
    };
    const { error: threadError } = await supabase.from('threads').insert(thread);
    if (threadError) { setError(threadError.message); return; }
    if (visibility === 'restricted') {
      const memberIds = [...new Set([myUserId, ...participants])];
      for (const user_id of memberIds) {
        const { error: participantError } = await supabase.from('thread_participants').insert({
          thread_id: id, team_id: teamId, user_id, added_by: myUserId,
        });
        if (participantError) { setError(participantError.message); return; }
      }
    }
    setCreating(false); setTitle(''); setParticipants([]); await load();
  };

  const active = threads.find((thread) => thread.id === threadId);
  if (threadId && active) return <GroupRoom key={active.id} thread={active} />;

  return <main style={{ flex: 1, padding: '28px', overflowY: 'auto' }}>
    <header style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
      <div><div className="display" style={{ fontSize: 30 }}>Threads</div><div style={{ color: 'var(--muted)', fontSize: 12 }}>Team conversations and focused work</div></div>
      <button className="btn-primary" style={{ marginLeft: 'auto' }} onClick={() => setCreating(true)}>New thread</button>
    </header>
    {error && <p style={{ color: 'var(--terracotta)' }}>{error}</p>}
    {creating && <section className="card" style={{ marginTop: 22, padding: 16, display: 'grid', gap: 10 }}>
      <label>Thread title<input aria-label="Thread title" value={title} onChange={(event) => setTitle(event.target.value)} autoFocus /></label>
      <label><input type="radio" checked={visibility === 'team'} onChange={() => setVisibility('team')} /> Team-visible</label>
      <label><input aria-label="Restricted to selected members" type="radio" checked={visibility === 'restricted'} onChange={() => setVisibility('restricted')} /> Restricted to selected members</label>
      <label>Kind <select value={kind} onChange={(event) => setKind(event.target.value as ThreadKind)}><option value="discussion">Discussion</option><option value="work">Work</option></select></label>
      {visibility === 'restricted' && <fieldset><legend>Participants</legend>{roster.filter(({ profile }) => profile.id !== myUserId).map(({ profile }) => <label key={profile.id}><input aria-label={profile.display_name} type="checkbox" checked={participants.includes(profile.id)} onChange={() => setParticipants((ids) => ids.includes(profile.id) ? ids.filter((id) => id !== profile.id) : [...ids, profile.id])} /> {profile.display_name}</label>)}</fieldset>}
      <div><button className="btn-primary" onClick={() => void create()}>Create thread</button><button className="btn-ghost" onClick={() => setCreating(false)}>Cancel</button></div>
    </section>}
    <div style={{ marginTop: 22, display: 'grid', gap: 8 }}>
      {threads.map((thread) => <Link key={thread.id} to={`../threads/${thread.id}`} style={{ color: 'inherit', textDecoration: 'none' }}><article className="card" style={{ padding: 14 }}><b>{thread.title}</b><span style={{ marginLeft: 8, color: 'var(--muted)', fontSize: 12 }}>{thread.visibility === 'restricted' ? 'Selected members' : 'Team'} · {thread.kind}</span></article></Link>)}
    </div>
  </main>;
}

/** Compatibility route only: the canonical thread id always comes from RLS. */
export function LegacyThreadRedirect({ privateThread = false }: { privateThread?: boolean }) {
  const { teamId } = useParams<{ teamId: string }>();
  const { myUserId } = useTeam();
  const navigate = useNavigate();
  const [missing, setMissing] = useState(false);
  useEffect(() => {
    if (!teamId) return;
    setMissing(false);
    let query = supabase.from('threads').select('id').eq('team_id', teamId);
    query = privateThread
      ? query.eq('owner_id', myUserId).eq('title', 'Private').eq('visibility', 'restricted')
      : query.eq('title', 'General').eq('visibility', 'team').eq('kind', 'discussion');
    // Two-argument `then`, not `.catch`. A Supabase query builder is a
    // THENABLE, not a Promise, so `.catch` does not exist on it — and the
    // production build is the only thing that says so: `npm run build` runs
    // `tsc -b`, which resolves tsconfig.app.json, while the gate's
    // `tsc --noEmit` does not. The typecheck lane was green while the image
    // could not be built at all.
    void query.maybeSingle().then(
      ({ data }) => {
        const id = (data as { id: string } | null)?.id;
        if (id) navigate(`/t/${teamId}/threads/${id}`, { replace: true });
        else setMissing(true);
      },
      () => setMissing(true),
    );
  }, [teamId, myUserId, privateThread, navigate]);
  if (missing) return <main style={{ flex: 1, padding: 28 }}>
    <p>{privateThread ? 'No private thread exists yet.' : 'The General thread is unavailable.'}</p>
    <Link to={`/t/${teamId}/threads`}>View threads</Link>
  </main>;
  return <main style={{ flex: 1, padding: 28 }}>Opening thread…</main>;
}
