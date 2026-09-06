import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { supabase } from '../lib/supabase';
import type { Membership, Team } from '../lib/types';
import { useAuth } from '../state/AuthContext';

interface TeamOption {
  team: Team;
  membership: Membership;
}

/** Pick an existing team, accept an invite, or create a new one. */
export function TeamGate() {
  const { session, signOut } = useAuth();
  const navigate = useNavigate();
  const [options, setOptions] = useState<TeamOption[] | null>(null);
  const [newName, setNewName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const uid = session?.user.id ?? '';

  const load = async () => {
    setOptions(null);
    setLoadError(null);
    // Not `.eq('status','active')`: an INVITED row is exactly what this
    // screen exists to show. But a row you LEFT must not appear at all —
    // status !== 'active' renders the ACCEPT INVITE branch below, so a team
    // you walked out of would offer to let you back in, and the update would
    // be refused by the transition guard.
    const { data: ms, error: membershipsError } = await supabase
      .from('memberships')
      .select('*')
      .eq('user_id', uid)
      .in('status', ['invited', 'active']);
    if (membershipsError) {
      setLoadError('We could not load your teams. Check your connection and try again.');
      return;
    }
    const memberships = (ms as Membership[] | null) ?? [];
    if (memberships.length === 0) {
      setOptions([]);
      return;
    }
    const { data: teams, error: teamsError } = await supabase
      .from('teams')
      .select('*')
      .in(
        'id',
        memberships.map((m) => m.team_id),
      );
    if (teamsError) {
      setLoadError('We found your memberships, but could not load the teams. Please try again.');
      return;
    }
    const byId = new Map(((teams as Team[] | null) ?? []).map((t) => [t.id, t]));
    setOptions(
      memberships
        .filter((m) => byId.has(m.team_id))
        .map((m) => ({ team: byId.get(m.team_id)!, membership: m })),
    );
  };

  useEffect(() => {
    if (uid) void load();
  }, [uid]); // eslint-disable-line react-hooks/exhaustive-deps

  const enter = (teamId: string) => {
    localStorage.setItem('comrade.teamId', teamId);
    navigate(`/t/${teamId}/threads`);
  };

  const acceptInvite = async (m: Membership) => {
    setBusy(true);
    const { error: err } = await supabase
      .from('memberships')
      .update({ status: 'active', joined_at: new Date().toISOString() })
      .eq('id', m.id);
    setBusy(false);
    if (err) setError(err.message);
    else enter(m.team_id);
  };

  const createTeam = async () => {
    const name = newName.trim();
    if (!name) return;
    setBusy(true);
    setError(null);
    const { data: team, error: tErr } = await supabase
      .from('teams')
      .insert({ name, created_by: uid })
      .select()
      .single();
    if (tErr || !team) {
      setBusy(false);
      setError(tErr?.message ?? 'Could not create team');
      return;
    }
    const { error: mErr } = await supabase.from('memberships').insert({
      team_id: (team as Team).id,
      user_id: uid,
      role: 'leader',
      status: 'active',
      joined_at: new Date().toISOString(),
    });
    setBusy(false);
    if (mErr) {
      setError(mErr.message);
      return;
    }
    localStorage.setItem('comrade.teamId', (team as Team).id);
    navigate(`/t/${(team as Team).id}/setup`);
  };

  return (
    <div
      style={{
        height: '100vh',
        overflowY: 'auto',
        display: 'flex',
        justifyContent: 'center',
        background: 'var(--canvas)',
      }}
    >
      <div className="gate-wrap" style={{ width: 'min(100%, 760px)', padding: '56px 24px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 24, alignItems: 'end', flexWrap: 'wrap' }}>
          <div>
            <div className="micro-label">Comrade / places to work</div>
            <div className="display" style={{ fontSize: 46, marginTop: 10 }}>
              Your teams
            </div>
            <div style={{ fontSize: 13, color: 'var(--muted)', marginTop: 8 }}>
              One room per team. Comrade is a careful member of each.
            </div>
          </div>
          <div className="mono" style={{ fontSize: 10, color: 'var(--muted)', letterSpacing: '.12em' }}>SELECT A ROOM</div>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 12, marginTop: 26 }}>
          {options === null && !loadError && (
            <div className="gate-loading" aria-label="Loading teams">
              <span className="skeleton" /><span className="skeleton" />
            </div>
          )}
          {loadError && (
            <div className="card" role="alert" style={{ padding: '16px 18px' }}>
              <div style={{ fontSize: 13, color: 'var(--text-soft)', lineHeight: 1.5 }}>
                {loadError}
              </div>
              <button className="btn-ink" style={{ marginTop: 12 }} onClick={() => void load()}>
                TRY AGAIN
              </button>
            </div>
          )}
          {options?.length === 0 && (
            <div className="card" style={{ padding: '22px 20px', fontSize: 13, color: 'var(--text-soft)', lineHeight: 1.6 }}>
              <strong style={{ color: 'var(--text)' }}>No room yet.</strong><br />
              Create one below, or ask a teammate&apos;s leader to invite you.
            </div>
          )}
          {options?.map(({ team, membership }) => (
            <div
              key={team.id}
              className="card"
              style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '15px 18px' }}
            >
              <div style={{ flex: 1 }}>
                <div className="display" style={{ fontSize: 20 }}>
                  {team.name}
                </div>
                <div className="mono" style={{ fontSize: 10, color: 'var(--muted)', marginTop: 4 }}>
                  {membership.role.toUpperCase()}
                  {membership.status === 'invited' ? ' · INVITED' : ''}
                </div>
              </div>
              {membership.status === 'active' ? (
                <button className="btn-ink" onClick={() => enter(team.id)}>
                  ENTER →
                </button>
              ) : (
                <button
                  className="btn-primary"
                  disabled={busy}
                  onClick={() => void acceptInvite(membership)}
                >
                  ACCEPT INVITE
                </button>
              )}
            </div>
          ))}
        </div>

        {!loadError && (
          <>
            <div className="micro-label" style={{ margin: '34px 0 12px' }}>
              Start a new team
            </div>
            <div className="composer">
              <input
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void createTeam();
                }}
                placeholder="Team name — e.g. MealShare"
              />
              <button className="btn-primary" disabled={busy} onClick={() => void createTeam()}>
                CREATE
              </button>
            </div>
          </>
        )}
        {error && <div style={{ marginTop: 12, fontSize: 12, color: 'var(--terracotta)' }}>{error}</div>}

        <button
          className="btn-ghost"
          style={{ marginTop: 30, paddingLeft: 0 }}
          onClick={() => void signOut()}
        >
          Sign out
        </button>
      </div>
    </div>
  );
}
