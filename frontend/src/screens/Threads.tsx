import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { supabase } from '../lib/supabase';
import { audienceOf, useThreads } from '../hooks/useThreads';
import { useTeam } from '../state/TeamContext';
import { GroupRoom } from './GroupRoom';

export function Threads() {
  const { threadId } = useParams<{ threadId: string }>();
  const navigate = useNavigate();
  const { team, roster, myUserId } = useTeam();
  const { threads, error, counts, createThread } = useThreads();
  const [creating, setCreating] = useState(false);
  const [composing, setComposing] = useState(false);
  const [chosen, setChosen] = useState<string[]>([]);
  const teamId = team?.id ?? '';

  const open = (id: string) => navigate(`/t/${teamId}/threads/${id}`);

  const createPublic = async () => {
    if (creating) return;
    setCreating(true);
    try {
      open((await createThread({ visibility: 'team' })).id);
    } finally {
      setCreating(false);
    }
  };

  const create = async () => {
    if (creating) return;
    setCreating(true);
    try {
      const thread = await createThread({ visibility: 'restricted', members: chosen });
      setComposing(false);
      setChosen([]);
      open(thread.id);
    } finally {
      setCreating(false);
    }
  };

  const active = threads.find((thread) => thread.id === threadId);
  if (threadId && active) {
    const participants = counts.get(active.id) ?? 0;
    // 🔴 This was `active.title === 'General'`. Every other public thread was
    // Comrade-only, so a team could open a thread everyone could see and find
    // they could not talk to each other in it — and renaming General silently
    // removed team chat from the one room that had it. What decides is who can
    // READ the thread: everyone, or the people in it.
    const allowTeamMessages = active.visibility === 'team' || participants > 1;
    return (
      <GroupRoom
        key={active.id}
        thread={active}
        allowTeamMessages={allowTeamMessages}
      />
    );
  }

  return <main className="workspace-page threads-page" style={{ flex: 1, padding: '28px', overflowY: 'auto' }}>
    <header className="workspace-heading" style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
      <div><div className="display" style={{ fontSize: 30 }}>Threads</div><div style={{ color: 'var(--muted)', fontSize: 12 }}>Team conversations and focused work</div></div>
      {/* Two buttons rather than one button and a mode. Creating a thread
          everyone can see is the common case and stays one click; choosing who
          is in one is the rarer case and is worth a step. */}
      <button className="btn-secondary" style={{ marginLeft: 'auto' }} disabled={creating} onClick={() => setComposing(true)}>New private thread</button>
      <button className="btn-primary" disabled={creating} onClick={() => void createPublic()}>New thread</button>
    </header>
    {error && <p style={{ color: 'var(--terracotta)' }}>{error}</p>}

    {composing && (
      <section className="card" data-new-thread style={{ marginTop: 18, padding: 16, display: 'grid', gap: 12 }}>
        <div className="micro-label">Who is in this thread</div>
        {(
          <div style={{ display: 'grid', gap: 6 }}>
            {roster.filter((m) => m.membership.user_id !== myUserId).length === 0 && (
              <span style={{ color: 'var(--faint)', fontSize: 12 }}>
                Nobody else on this team yet — it will be private to you.
              </span>
            )}
            {roster
              .filter((m) => m.membership.user_id !== myUserId)
              .map((m) => (
                <label key={m.membership.user_id} style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 13 }}>
                  <input
                    type="checkbox"
                    checked={chosen.includes(m.membership.user_id)}
                    onChange={(e) => setChosen((c) => (
                      e.target.checked
                        ? [...c, m.membership.user_id]
                        : c.filter((id) => id !== m.membership.user_id)
                    ))}
                  />
                  {m.profile.display_name}
                </label>
              ))}
          </div>
        )}
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn-primary" disabled={creating} onClick={() => void create()}>Create</button>
          <button className="btn-ghost" onClick={() => setComposing(false)}>Cancel</button>
        </div>
      </section>
    )}

    <div className="thread-list" style={{ marginTop: 22, display: 'grid', gap: 8 }}>
      {threads.map((thread) => <Link key={thread.id} to={`../threads/${thread.id}`} style={{ color: 'inherit', textDecoration: 'none' }}><article className="card" style={{ padding: 14 }}><b>{thread.title}</b><span style={{ marginLeft: 8, color: 'var(--muted)', fontSize: 12 }}>{audienceOf(thread, counts.get(thread.id) ?? 0)} · {thread.kind}</span></article></Link>)}
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
  if (missing) return <main className="workspace-page" style={{ flex: 1, padding: 28 }}>
    <p>{privateThread ? 'No private thread exists yet.' : 'The General thread is unavailable.'}</p>
    <Link to={`/t/${teamId}/threads`}>View threads</Link>
  </main>;
  return <main className="workspace-page" style={{ flex: 1, padding: 28 }}>Opening thread…</main>;
}
