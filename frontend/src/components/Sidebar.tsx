import { useCallback, useEffect, useState } from 'react';
import { NavLink, useNavigate } from 'react-router-dom';
import { supabase } from '../lib/supabase';
import type { MemoryCompilation, Milestone } from '../lib/types';
import { daysUntil } from '../lib/format';
import { useAuth } from '../state/AuthContext';
import { useTeam } from '../state/TeamContext';
import { AiOrb, Avatar, OrbLogo } from './Avatar';
import { useThreads } from '../hooks/useThreads';

const NAV_ITEMS = [
  { to: 'tasks', icon: '01', label: 'Tasks' },
  { to: 'wiki', icon: '02', label: 'Team wiki' },
  { to: 'docs', icon: '03', label: 'Documents' },
] as const;

const navBase: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 9,
  width: '100%',
  border: 'none',
  textAlign: 'left',
  cursor: 'pointer',
  fontSize: 13,
  borderRadius: 7,
  padding: '7px 10px',
  textDecoration: 'none',
};

export function Sidebar({ onNavigate }: { onNavigate?: () => void } = {}) {
  const { team, roster, myUserId } = useTeam();
  const { signOut } = useAuth();
  const navigate = useNavigate();
  const teamId = team?.id ?? '';
  const { threads, error: threadError, createPublicThread } = useThreads();
  const [creatingThread, setCreatingThread] = useState(false);

  const [lastCompile, setLastCompile] = useState<MemoryCompilation | null>(null);
  const [nextMilestone, setNextMilestone] = useState<Milestone | null>(null);

  const loadSignals = useCallback(async () => {
    if (!teamId) return;
    const [{ data: compiles }, { data: mss }] =
      await Promise.all([
      supabase
        .from('memory_compilations')
        .select('*')
        .eq('team_id', teamId)
        .eq('status', 'done')
        .order('started_at', { ascending: false })
        .limit(1),
      supabase
        .from('milestones')
        .select('*')
        .eq('team_id', teamId)
        .gte('due_at', new Date().toISOString())
        .order('due_at', { ascending: true })
        .limit(1),
    ]);
    setLastCompile(((compiles as MemoryCompilation[] | null) ?? [])[0] ?? null);
    setNextMilestone(((mss as Milestone[] | null) ?? [])[0] ?? null);
  }, [teamId]);

  useEffect(() => {
    loadSignals();
  }, [loadSignals]);

  const createThread = async () => {
    if (creatingThread) return;
    setCreatingThread(true);
    try {
      const thread = await createPublicThread();
      navigate(`/t/${teamId}/threads/${thread.id}`);
    } finally {
      setCreatingThread(false);
    }
  };

  return (
    <nav
      onClick={onNavigate}
      style={{
        width: 250,
        maxWidth: '85vw',
        flex: 'none',
         background: 'var(--surface)',
         color: 'var(--ink)',
        display: 'flex',
        flexDirection: 'column',
        padding: '22px 14px 16px',
        position: 'relative',
        overflowY: 'auto',
      }}
    >
      <div style={{ padding: '0 10px 6px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginBottom: 16 }}>
          <OrbLogo size={28} />
          <span style={{ fontSize: 15, fontWeight: 600, letterSpacing: '0.02em' }}>comrade</span>
        </div>
        <div className="display" style={{ fontSize: 26, letterSpacing: '0.01em' }}>
          {team?.name ?? '…'}
        </div>
        <button
          onClick={() => {
            localStorage.removeItem('comrade.teamId');
            navigate('/teams');
          }}
          style={{
            border: 'none',
            background: 'transparent',
            padding: 0,
            marginTop: 6,
            fontSize: 10.5,
            letterSpacing: '0.14em',
            textTransform: 'uppercase',
            color: 'var(--ink-muted)',
            cursor: 'pointer',
          }}
          title="Switch team"
        >
           switch team
        </button>
      </div>

      <NavLink
        to="thread"
        style={({ isActive }) => ({
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          margin: '16px 6px 6px',
          padding: 12,
          border: '1px solid rgba(241,239,234,0.12)',
           background: isActive ? 'rgba(118,85,121,0.14)' : 'rgba(118,85,121,0.06)',
          borderRadius: 12,
          cursor: 'pointer',
          textAlign: 'left',
           color: 'var(--ink)',
          textDecoration: 'none',
        })}
      >
        <AiOrb size={34} breathing />
        <span>
          <span style={{ display: 'block', fontSize: 13, fontWeight: 600 }}>Comrade</span>
          <span
            style={{
              display: 'block',
              fontSize: 10.5,
               color: 'var(--terracotta)',
              marginTop: 2,
              animation: 'tickerPulse 3.4s ease-in-out infinite',
            }}
          >
            ready in your thread
          </span>
        </span>
      </NavLink>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 2, marginTop: 14 }}>
        {NAV_ITEMS.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            style={({ isActive }) => ({
              ...navBase,
              fontWeight: isActive ? 600 : 400,
               color: isActive ? 'var(--ink)' : 'var(--text-soft)',
               background: isActive ? 'rgba(118,85,121,0.12)' : 'transparent',
            })}
          >
            <span style={{ width: 16, textAlign: 'center', opacity: 0.7 }}>{item.icon}</span>
            {item.label}
          </NavLink>
        ))}
      </div>

      <div style={{ margin: '22px 10px 8px', display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ flex: 1, fontSize: 9.5, fontWeight: 600, letterSpacing: '0.18em', color: 'var(--ink-faint)', textTransform: 'uppercase' }}>
          Threads
        </span>
        <button
          type="button"
          aria-label="New thread"
          disabled={creatingThread}
          onClick={() => void createThread()}
           style={{ border: 0, borderRadius: 5, background: 'rgba(118,85,121,0.12)', color: 'var(--ink)', cursor: 'pointer', padding: '2px 7px', fontSize: 15 }}
        >
          +
        </button>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
        {threads.map((thread) => (
          <NavLink
            key={thread.id}
            to={`threads/${thread.id}`}
            title={thread.visibility === 'restricted' ? 'Selected members' : 'Team-visible'}
            style={({ isActive }) => ({
              ...navBase,
               color: isActive ? 'var(--ink)' : 'var(--text-soft)',
               background: isActive ? 'rgba(118,85,121,0.12)' : 'transparent',
              overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis',
            })}
          >
           <span aria-hidden style={{ color: 'var(--ink-faint)', fontFamily: 'var(--mono)', fontSize: 11 }}>{thread.visibility === 'restricted' ? 'R' : 'T'}</span>
            {thread.title}
          </NavLink>
        ))}
        {threadError && <span style={{ padding: '0 10px', color: 'var(--terracotta-soft)', fontSize: 11 }}>Could not load threads</span>}
      </div>

      <div
        style={{
          margin: '22px 10px 9px',
          fontSize: 9.5,
          fontWeight: 600,
          letterSpacing: '0.18em',
          color: 'var(--ink-faint)',
          textTransform: 'uppercase',
        }}
      >
        Members
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
        {roster.map(({ membership, profile }) => (
          <div
            key={membership.id}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 9,
              padding: '5px 10px',
              fontSize: 12.5,
              color: 'var(--ink-roster)',
            }}
          >
            <Avatar userId={profile.id} name={profile.display_name} size={22} />
            {profile.display_name}
            <span
              style={{
                marginLeft: 'auto',
                fontSize: 9.5,
                letterSpacing: '0.1em',
                textTransform: 'uppercase',
                color: 'var(--ink-faint)',
              }}
            >
              {profile.id === myUserId
                ? 'you'
                : membership.role === 'leader'
                  ? 'lead'
                  : ''}
            </span>
          </div>
        ))}
      </div>

      <div
        style={{
          marginTop: 'auto',
          padding: '12px 10px 10px',
          borderTop: '1px solid rgba(241,239,234,0.1)',
        }}
      >
        <div
          style={{
            fontSize: 9.5,
            letterSpacing: '0.18em',
            textTransform: 'uppercase',
            color: 'var(--ink-faint)',
            marginBottom: 8,
          }}
        >
          Live signals
        </div>
        <div
          className="mono"
          style={{
            display: 'flex',
            flexDirection: 'column',
            gap: 6,
            fontSize: 11,
            lineHeight: 1.45,
             color: 'var(--text-soft)',
          }}
        >
          {lastCompile && (
            <div>
              <span style={{ color: 'var(--terracotta-soft)' }}>+</span> memory compiled · +
              {lastCompile.entries_added} facts
            </div>
          )}
          {nextMilestone?.due_at && (
            <div>
               <span style={{ color: 'var(--ink-faint)' }}>•</span>{' '}
              {nextMilestone.title.toLowerCase()} in {daysUntil(nextMilestone.due_at)} days
            </div>
          )}
        </div>
        <NavLink
          to="setup"
          style={({ isActive }) => ({
            ...navBase,
            marginTop: 10,
            fontWeight: isActive ? 600 : 400,
             color: isActive ? 'var(--ink)' : 'var(--text-soft)',
             background: isActive ? 'rgba(118,85,121,0.12)' : 'transparent',
          })}
        >
           <span style={{ width: 16, textAlign: 'center', opacity: 0.7 }}>04</span> Project setup
        </NavLink>
        {/* Sits with Sign out rather than in the main nav: leaving a team is
            the same class of action, and neither belongs beside Tasks. */}
        <NavLink
          to="team"
          style={({ isActive }) => ({
            ...navBase,
            fontWeight: isActive ? 600 : 400,
             color: isActive ? 'var(--ink)' : 'var(--text-soft)',
             background: isActive ? 'rgba(118,85,121,0.12)' : 'transparent',
          })}
        >
           <span style={{ width: 16, textAlign: 'center', opacity: 0.7 }}>05</span> Membership
        </NavLink>
        <button
          onClick={() => signOut()}
           style={{ ...navBase, background: 'transparent', color: 'var(--text-soft)', marginTop: 2 }}
        >
           <span style={{ width: 16, textAlign: 'center', opacity: 0.7 }}>↗</span> Sign out
        </button>
      </div>
    </nav>
  );
}
