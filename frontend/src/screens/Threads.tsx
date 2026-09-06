import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { supabase } from '../lib/supabase';
import { useThreads } from '../hooks/useThreads';
import { useTeam } from '../state/TeamContext';
import { GroupRoom } from './GroupRoom';

export function Threads() {
  const { threadId } = useParams<{ threadId: string }>();
  const navigate = useNavigate();
  const { team } = useTeam();
  const { threads, error, createPublicThread } = useThreads();
  const [creating, setCreating] = useState(false);
  const teamId = team?.id ?? '';

  const create = async () => {
    if (creating) return;
    setCreating(true);
    try {
      const thread = await createPublicThread();
      navigate(`/t/${teamId}/threads/${thread.id}`);
    } finally {
      setCreating(false);
    }
  };

  const active = threads.find((thread) => thread.id === threadId);
  if (threadId && active) return <GroupRoom key={active.id} thread={active} />;

  return <main style={{ flex: 1, padding: '28px', overflowY: 'auto' }}>
    <header style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
      <div><div className="display" style={{ fontSize: 30 }}>Threads</div><div style={{ color: 'var(--muted)', fontSize: 12 }}>Team conversations and focused work</div></div>
      <button className="btn-primary" style={{ marginLeft: 'auto' }} disabled={creating} onClick={() => void create()}>New thread</button>
    </header>
    {error && <p style={{ color: 'var(--terracotta)' }}>{error}</p>}
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
