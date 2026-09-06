import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Avatar } from '../components/Avatar';
import { AgentApiError, askToLeave, downloadTeamExport } from '../lib/agentApi';
import { leaveConsequenceText } from '../lib/leaving';
import { supabase } from '../lib/supabase';
import { useTeam } from '../state/TeamContext';

/**
 * Membership — the screen for the things a team does to itself.
 *
 * Deliberately not folded into Setup, which is a three-step onboarding wizard;
 * leaving a team eighteen months later is not step four of getting started.
 *
 * The whole screen is arranged around one claim: your participation is yours.
 * You can take your history and go, at any time, without asking. Nobody has a
 * button that removes you — the only thing a teammate can do is put a question
 * in your private thread.
 */
export function Team() {
  const { team, roster, myUserId, loading } = useTeam();
  const navigate = useNavigate();
  const teamId = team?.id ?? '';

  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmLeave, setConfirmLeave] = useState(false);
  const [asking, setAsking] = useState<string | null>(null);
  const [reason, setReason] = useState('');

  const me = roster.find((r) => r.membership.user_id === myUserId);
  const iAmLeader = me?.membership.role === 'leader';
  // Not computed while the roster is loading. An empty roster reads as
  // activeCount 0, which leaveOutcome deliberately rounds to "you are the last
  // member" — the right default for an unknown, but shown for half a second on
  // every load it is just a false alarm about closing the team.
  const consequence = loading
    ? null
    : leaveConsequenceText({ isLeader: iAmLeader, activeCount: roster.length });

  const say = (e: unknown) =>
    setError(e instanceof AgentApiError ? e.message : (e as Error).message);

  const exportTeam = async () => {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      await downloadTeamExport(teamId);
      setNote('Exported — everything you can read, as JSON.');
    } catch (e) {
      say(e);
    } finally {
      setBusy(false);
    }
  };

  const leave = async () => {
    setBusy(true);
    setError(null);
    // The write the whole feature rests on, and it is one UPDATE of your own
    // row. No endpoint, no approval: au_memberships_update already lets you
    // write your own membership, and the transition guard allows active→left
    // for exactly the member it belongs to.
    const { error: err } = await supabase
      .from('memberships')
      .update({ status: 'left', left_at: new Date().toISOString() })
      .eq('team_id', teamId)
      .eq('user_id', myUserId);
    setBusy(false);
    if (err) {
      setError(err.message);
      return;
    }
    localStorage.removeItem('comrade.teamId');
    navigate('/teams');
  };

  const ask = async (memberId: string) => {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      await askToLeave(teamId, memberId, reason);
      setNote('Asked. It is their decision — the card is in their private thread, not yours.');
      setAsking(null);
      setReason('');
      // Deliberately no roster refresh: nothing has changed yet, and reloading
      // would suggest it had.
    } catch (e) {
      say(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="page-scroll team-page">
      <div className="content-column" style={{ maxWidth: 580, margin: '0 auto', padding: '52px 32px 60px' }}>
        <div className="display" style={{ fontSize: 40, lineHeight: 1.05 }}>
          Membership
        </div>
        <div style={{ fontSize: 13, color: 'var(--muted)', marginTop: 10, lineHeight: 1.6 }}>
          Your participation is yours. Leave whenever you like, and take the history with you.
        </div>

        <div className="micro-label" style={{ margin: '34px 0 12px' }}>
          Who is here
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {roster.map(({ membership, profile }) => (
            <div key={membership.id} className="card" style={{ padding: '14px 17px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 11 }}>
                <Avatar userId={profile.id} name={profile.display_name} size={28} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13.5, fontWeight: 600 }}>
                    {profile.display_name}
                    {profile.id === myUserId && (
                      <span style={{ color: 'var(--faint)', fontWeight: 400 }}> · you</span>
                    )}
                  </div>
                  <div
                    className="mono"
                    style={{ fontSize: 10, color: 'var(--muted)', marginTop: 3 }}
                  >
                    {membership.role.toUpperCase()}
                  </div>
                </div>
                {profile.id !== myUserId && (
                  <button
                    className="btn-ghost"
                    disabled={busy}
                    onClick={() => {
                      setAsking(asking === profile.id ? null : profile.id);
                      setReason('');
                    }}
                  >
                    Ask to leave
                  </button>
                )}
              </div>
              {asking === profile.id && (
                <div style={{ marginTop: 12, paddingLeft: 39 }}>
                  <div style={{ fontSize: 12, color: 'var(--muted)', lineHeight: 1.55 }}>
                    This does not remove {profile.display_name}. It puts the question in their
                    private thread, where only they can answer it — you will not see the card.
                  </div>
                  <input
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    placeholder="Why (optional) — they will see this"
                    maxLength={280}
                    style={{
                      width: '100%',
                      marginTop: 10,
                      border: '1px solid rgba(35,33,48,0.22)',
                      borderRadius: 3,
                      padding: '9px 12px',
                      fontSize: 12.5,
                      fontFamily: 'inherit',
                      outline: 'none',
                      background: '#fff',
                    }}
                  />
                  <button
                    className="btn-ink"
                    disabled={busy}
                    style={{ marginTop: 9 }}
                    onClick={() => void ask(profile.id)}
                  >
                    SEND THE QUESTION
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>

        <div className="micro-label" style={{ margin: '34px 0 12px' }}>
          Take it with you
        </div>
        <div className="card" style={{ padding: '17px 19px' }}>
          <div style={{ fontSize: 12.5, color: 'var(--text-soft)', lineHeight: 1.6 }}>
            One JSON file: the wiki, tasks, documents, the group room, and your own private
            thread. A teammate's private thread is not in it — it was never yours to read.
          </div>
          <button
            className="btn-ink"
            disabled={busy}
            style={{ marginTop: 12 }}
            onClick={() => void exportTeam()}
          >
            EXPORT
          </button>
        </div>

        <div className="micro-label" style={{ margin: '34px 0 12px' }}>
          Leaving
        </div>
        <div className="card" style={{ padding: '17px 19px' }}>
          <div style={{ fontSize: 12.5, color: 'var(--text-soft)', lineHeight: 1.6 }}>
            {consequence ?? '…'}
          </div>
          {consequence !== null && confirmLeave ? (
            <div style={{ display: 'flex', gap: 9, marginTop: 13, flexWrap: 'wrap' }}>
              <button className="btn-primary" disabled={busy} onClick={() => void leave()}>
                YES, LEAVE {team?.name?.toUpperCase() ?? ''}
              </button>
              <button className="btn-ghost" onClick={() => setConfirmLeave(false)}>
                Stay
              </button>
            </div>
          ) : (
            <button
              className="btn-ghost"
              disabled={consequence === null}
              style={{ marginTop: 12, paddingLeft: 0, color: 'var(--terracotta)' }}
              onClick={() => setConfirmLeave(true)}
            >
              Leave this team
            </button>
          )}
        </div>

        {note && (
          <div style={{ marginTop: 16, fontSize: 12.5, color: 'var(--text-soft)' }}>{note}</div>
        )}
        {error && (
          <div style={{ marginTop: 16, fontSize: 12.5, color: 'var(--terracotta)' }}>{error}</div>
        )}
      </div>
    </main>
  );
}
