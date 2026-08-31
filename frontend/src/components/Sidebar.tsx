import { useCallback, useEffect, useState } from 'react';
import { NavLink, useNavigate } from 'react-router-dom';
import { supabase } from '../lib/supabase';
import type { MemoryCompilation, Milestone } from '../lib/types';
import { daysUntil } from '../lib/format';
import { useAuth } from '../state/AuthContext';
import { useTeam } from '../state/TeamContext';
import { useTeamRealtime } from '../hooks/useRealtime';
import { Avatar } from './Avatar';

const NAV_ITEMS = [
  { to: 'room', icon: '#', label: 'Group room' },
  { to: 'tasks', icon: '☑', label: 'Tasks' },
  { to: 'wiki', icon: '✦', label: 'Team wiki' },
  { to: 'docs', icon: '▤', label: 'Documents' },
  { to: 'inbox', icon: '✳', label: 'Consent inbox' },
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

  // null = we do not know yet. The count is a HEAD request that is routinely
  // aborted (a re-render cancels it), and `count ?? 0` turned every one of
  // those into a confident "consent queue clear" — the D2 defect, in the one
  // place it costs the most: a card nobody knows is waiting. Since D4 that
  // card may be a teammate asking you to leave.
  const [pendingCount, setPendingCount] = useState<number | null>(null);
  const [lastCompile, setLastCompile] = useState<MemoryCompilation | null>(null);
  const [nextMilestone, setNextMilestone] = useState<Milestone | null>(null);

  const loadSignals = useCallback(async () => {
    if (!teamId) return;
    const [{ count, error: countErr }, { data: compiles }, { data: mss }] =
      await Promise.all([
      supabase
        .from('consent_queue')
        .select('id', { count: 'exact', head: true })
        .eq('team_id', teamId)
        .eq('status', 'pending')
        // Past its 7-day backstop it cannot be approved, so counting it as
        // "awaiting your key" sends a member to an inbox that has nothing
        // they can act on. Filter server-side: the count is a head request
        // and never fetches the rows to filter locally.
        .or(`expires_at.is.null,expires_at.gt.${new Date().toISOString()}`),
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
    // Keep the last known figure rather than overwriting it with a zero we
    // did not receive.
    if (!countErr) setPendingCount(count ?? 0);
    setLastCompile(((compiles as MemoryCompilation[] | null) ?? [])[0] ?? null);
    setNextMilestone(((mss as Milestone[] | null) ?? [])[0] ?? null);
  }, [teamId]);

  useEffect(() => {
    loadSignals();
  }, [loadSignals]);
  useTeamRealtime('consent_queue', teamId, loadSignals);

  return (
    <nav
      onClick={onNavigate}
      style={{
        width: 250,
        maxWidth: '85vw',
        flex: 'none',
        background: 'var(--ink)',
        color: 'var(--paper)',
        display: 'flex',
        flexDirection: 'column',
        padding: '22px 14px 16px',
        position: 'relative',
        overflowY: 'auto',
      }}
    >
      <div style={{ padding: '0 10px 6px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginBottom: 16 }}>
          <span
            style={{
              width: 28,
              height: 28,
              flex: 'none',
              borderRadius: 9,
              background: '#FBF9F4',
              boxShadow: '0 0 0 1px rgba(241,239,234,0.14)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: 'var(--ink)',
              fontSize: 15,
            }}
          >
            ◈
          </span>
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
          switch team ↺
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
          background: isActive ? 'rgba(228,121,91,0.14)' : 'rgba(228,121,91,0.07)',
          borderRadius: 12,
          cursor: 'pointer',
          textAlign: 'left',
          color: 'var(--paper)',
          textDecoration: 'none',
        })}
      >
        <span
          className="orb breathing"
          style={{ width: 34, height: 34, fontSize: 14 }}
        >
          ◈
        </span>
        <span>
          <span style={{ display: 'block', fontSize: 13, fontWeight: 600 }}>Comrade</span>
          <span
            style={{
              display: 'block',
              fontSize: 10.5,
              color: '#D9A18E',
              marginTop: 2,
              animation: 'tickerPulse 3.4s ease-in-out infinite',
            }}
          >
            {pendingCount === null
              ? 'watching'
              : pendingCount > 0
                ? `watching · ${pendingCount} awaiting key`
                : 'watching · all clear'}
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
              color: isActive ? 'var(--paper)' : '#A6A1B3',
              background: isActive ? 'rgba(241,239,234,0.1)' : 'transparent',
            })}
          >
            <span style={{ width: 16, textAlign: 'center', opacity: 0.7 }}>{item.icon}</span>
            {item.label}
            {item.to === 'inbox' && pendingCount !== null && pendingCount > 0 && (
              <span
                style={{
                  marginLeft: 'auto',
                  minWidth: 17,
                  height: 17,
                  borderRadius: 9,
                  background: 'var(--terracotta)',
                  color: '#fff',
                  fontSize: 10,
                  fontWeight: 700,
                  display: 'inline-flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  padding: '0 5px',
                }}
              >
                {pendingCount}
              </span>
            )}
          </NavLink>
        ))}
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
            color: '#908B9E',
          }}
        >
          {lastCompile && (
            <div>
              <span style={{ color: 'var(--terracotta-soft)' }}>▲</span> memory compiled · +
              {lastCompile.entries_added} facts
            </div>
          )}
          <div>
            <span style={{ color: 'var(--lavender)' }}>✳</span>{' '}
            {pendingCount === null
              ? 'checking the consent queue'
              : pendingCount === 0
                ? 'consent queue clear'
                : `${pendingCount} consent${pendingCount > 1 ? 's' : ''} awaiting key`}
          </div>
          {nextMilestone?.due_at && (
            <div>
              <span style={{ color: 'var(--ink-faint)' }}>◆</span>{' '}
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
            color: isActive ? 'var(--paper)' : '#A6A1B3',
            background: isActive ? 'rgba(241,239,234,0.1)' : 'transparent',
          })}
        >
          <span style={{ width: 16, textAlign: 'center', opacity: 0.7 }}>⚙</span> Project setup
        </NavLink>
        {/* Sits with Sign out rather than in the main nav: leaving a team is
            the same class of action, and neither belongs beside Tasks. */}
        <NavLink
          to="team"
          style={({ isActive }) => ({
            ...navBase,
            fontWeight: isActive ? 600 : 400,
            color: isActive ? 'var(--paper)' : '#A6A1B3',
            background: isActive ? 'rgba(241,239,234,0.1)' : 'transparent',
          })}
        >
          <span style={{ width: 16, textAlign: 'center', opacity: 0.7 }}>◇</span> Membership
        </NavLink>
        <button
          onClick={() => signOut()}
          style={{ ...navBase, background: 'transparent', color: '#A6A1B3', marginTop: 2 }}
        >
          <span style={{ width: 16, textAlign: 'center', opacity: 0.7 }}>↦</span> Sign out
        </button>
      </div>
    </nav>
  );
}
