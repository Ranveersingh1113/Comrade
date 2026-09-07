import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { supabase } from '../lib/supabase';
import { daysUntil, firstNameOf, shortDate } from '../lib/format';
import type { ContributionRow, Milestone } from '../lib/types';
import { isConfirmedEmpty } from '../lib/listState';
import { useTeam } from '../state/TeamContext';
import { taskCell, taskMark, taskPill, useTasks } from '../hooks/useTasks';
import { taskAffordance } from '../lib/taskFlow';
import { Avatar } from '../components/Avatar';

export function Tasks() {
  const { team, roster, myUserId } = useTeam();
  const { tasks, error, loading, advance, create } = useTasks();
  const [milestones, setMilestones] = useState<Milestone[]>([]);
  const [contrib, setContrib] = useState<Map<string, ContributionRow>>(new Map());
  const [newTitle, setNewTitle] = useState('');
  const [newAssignee, setNewAssignee] = useState('');
  const teamId = team?.id ?? '';

  useEffect(() => {
    if (!teamId) return;
    void supabase
      .from('milestones')
      .select('*')
      .eq('team_id', teamId)
      .order('due_at')
      .then(({ data }) => setMilestones((data as Milestone[] | null) ?? []));
    void supabase
      .from('contribution_v')
      .select('*')
      .eq('team_id', teamId)
      .then(({ data }) =>
        setContrib(
          new Map(((data as ContributionRow[] | null) ?? []).map((r) => [r.user_id, r])),
        ),
      );
  }, [teamId, tasks.length]);

  const propose = async () => {
    const title = newTitle.trim();
    if (!title) return;
    await create(title, newAssignee || null, null);
    setNewTitle('');
  };

  const unassigned = tasks.filter(
    (t) => !t.assignee_id || !roster.some((r) => r.profile.id === t.assignee_id),
  );

  return (
    <main className="page-scroll tasks-page">
      <header className="screen-header">
        <div className="display">Tasks &amp; contribution</div>
        <div className="sub">
          proposed → confirmed → in progress → done · only the assignee confirms · no rankings
        </div>
      </header>
      <div className="content-column" style={{ maxWidth: 780, margin: '0 auto', padding: '24px 32px 44px' }}>
        {milestones.length > 0 && (
          <div style={{ display: 'flex', gap: 14, marginBottom: 22, flexWrap: 'wrap' }}>
            {milestones.map((ms) => {
              const upcoming = ms.due_at && new Date(ms.due_at) > new Date();
              const color = upcoming ? 'var(--terracotta)' : 'var(--plum)';
              return (
                <div
                  key={ms.id}
                  className="card"
                  style={{
                    flex: 1,
                    minWidth: 230,
                    display: 'flex',
                    alignItems: 'center',
                    gap: 13,
                    boxShadow: 'var(--shadow-input)',
                    padding: '13px 17px',
                  }}
                >
                  <span className="display" style={{ fontSize: 30, color }}>
                    <span aria-hidden>+</span>
                  </span>
                  <span style={{ flex: 1 }}>
                    <span style={{ display: 'block', fontSize: 13, fontWeight: 700 }}>
                      {ms.title}
                    </span>
                    <span
                      className="mono"
                      style={{ display: 'block', fontSize: 10.5, color: 'var(--muted)', marginTop: 2 }}
                    >
                      {ms.due_at
                        ? `${shortDate(ms.due_at)} · in ${daysUntil(ms.due_at)} days`
                        : 'no date'}
                    </span>
                  </span>
                  <span
                    className="mono"
                    style={{ fontSize: 9, fontWeight: 700, letterSpacing: '0.14em', color }}
                  >
                    MILESTONE
                  </span>
                </div>
              );
            })}
          </div>
        )}

        {error && (
          <div style={{ fontSize: 12.5, color: 'var(--terracotta)', marginBottom: 14 }}>{error}</div>
        )}

        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          {roster.map(({ profile }) => {
            const mine = tasks.filter((t) => t.assignee_id === profile.id);
            const done = mine.filter((t) => t.status === 'done').length;
            const c = contrib.get(profile.id);
            return (
              <div key={profile.id} className="card fade-up" style={{ padding: '20px 22px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 13 }}>
                  <Avatar userId={profile.id} name={profile.display_name} size={36} />
                  <div style={{ flex: 1 }}>
                    <div className="display" style={{ fontSize: 19 }}>
                      {profile.display_name}
                    </div>
                    <div className="mono" style={{ fontSize: 10.5, color: 'var(--muted)', marginTop: 4 }}>
                      {c && c.github_events > 0
                        ? `${c.github_events} github events`
                        : profile.github_username
                          ? `@${profile.github_username}`
                          : 'no github activity yet'}
                    </div>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                    <span style={{ display: 'flex', gap: 4 }}>
                      {mine.map((t) => {
                        const cc = taskCell(t.status);
                        return (
                          <span
                            key={t.id}
                            title={`${t.title} — ${t.status}`}
                            style={{
                              width: 13,
                              height: 13,
                              borderRadius: 2,
                              background: cc.bg,
                              border: `1px ${cc.style} ${cc.border}`,
                            }}
                          />
                        );
                      })}
                    </span>
                    <span className="mono" style={{ fontSize: 11, color: 'var(--text-soft)' }}>
                      {done}/{mine.length} done
                    </span>
                  </div>
                </div>
                <div
                  style={{
                    display: 'flex',
                    flexDirection: 'column',
                    gap: 3,
                    marginTop: 14,
                    paddingLeft: 49,
                  }}
                >
                  {isConfirmedEmpty(error, loading, mine.length) && (
                    <div style={{ fontSize: 12, color: 'var(--faint)', fontStyle: 'italic' }}>
                      No tasks yet.
                    </div>
                  )}
                  {mine.map((t) => {
                    const cc = taskCell(t.status);
                    const p = taskPill(t.status);
                    return (
                      <div
                        key={t.id}
                        style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '4px 0' }}
                      >
                        <span
                          style={{
                            width: 15,
                            height: 15,
                            flex: 'none',
                            borderRadius: 3,
                            border: `1.5px ${cc.style} ${cc.border}`,
                            background: cc.bg,
                            color: 'var(--paper)',
                            fontSize: 9,
                            display: 'inline-flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                          }}
                        >
                          {taskMark(t.status)}
                        </span>
                        <span
                          style={{
                            fontSize: 13,
                            color: t.status === 'done' ? 'var(--faint)' : 'var(--text-body)',
                            textDecoration: t.status === 'done' ? 'line-through' : 'none',
                          }}
                        >
                          {t.title}
                        </span>
                        <span
                          className="mono"
                          style={{
                            fontSize: 8.5,
                            letterSpacing: '0.12em',
                            color: p.color,
                            border: `1px solid ${p.border}`,
                            borderRadius: 2,
                            padding: '2px 6px',
                          }}
                        >
                          {p.label}
                        </span>
                        {/* The work happens somewhere. Before this the board
                            and the thread were two unconnected accounts of
                            the same job, with no way to get from one to the
                            other. */}
                        {t.thread_id && (
                          <Link
                            to={`/t/${teamId}/threads/${t.thread_id}`}
                            className="mono"
                            style={{
                              fontSize: 8.5,
                              letterSpacing: '0.12em',
                              color: 'var(--muted)',
                              textDecoration: 'none',
                              border: '1px solid var(--border-soft)',
                              borderRadius: 2,
                              padding: '2px 6px',
                            }}
                          >
                            OPEN THREAD
                          </Link>
                        )}
                        {/* Only the assignee advances — mirrors the DB trigger. */}
                        {(() => {
                          const a = taskAffordance(t, myUserId, firstNameOf(profile.display_name));
                          switch (a.kind) {
                            case 'confirm':
                              return (
                                <button
                                  onClick={() => void advance(t)}
                                  className="btn-primary"
                                  style={{ marginLeft: 'auto', fontSize: 10.5, padding: '5px 11px' }}
                                >
                                  CONFIRM — IT'S YOURS
                                </button>
                              );
                            case 'start':
                              return (
                                <button
                                  onClick={() => void advance(t)}
                                  className="btn-secondary"
                                  style={{
                                    marginLeft: 'auto',
                                    fontSize: 10.5,
                                    fontWeight: 600,
                                    letterSpacing: '0.06em',
                                    padding: '5px 11px',
                                    borderRadius: 2,
                                  }}
                                >
                                  START
                                </button>
                              );
                            case 'finish':
                              return (
                                <button
                                  onClick={() => void advance(t)}
                                  style={{
                                    marginLeft: 'auto',
                                     border: '1px solid rgba(199,104,99,0.5)',
                                     background: 'var(--card)',
                                    color: 'var(--terracotta)',
                                    fontSize: 10.5,
                                    fontWeight: 600,
                                    letterSpacing: '0.06em',
                                    borderRadius: 2,
                                    padding: '5px 11px',
                                    cursor: 'pointer',
                                  }}
                                >
                                  MARK DONE
                                </button>
                              );
                            case 'wait':
                              return (
                                <span
                                  style={{
                                    marginLeft: 'auto',
                                    fontSize: 11,
                                    fontStyle: 'italic',
                                    color: 'var(--faint)',
                                  }}
                                >
                                  waiting on {a.assigneeName} to confirm
                                </span>
                              );
                            default:
                              return null;
                          }
                        })()}
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })}

          {unassigned.length > 0 && (
            <div className="card" style={{ padding: '16px 22px' }}>
              <div className="micro-label" style={{ marginBottom: 10 }}>
                Unassigned
              </div>
              {unassigned.map((t) => (
                <div key={t.id} style={{ fontSize: 13, color: 'var(--text-soft)', padding: '3px 0' }}>
                  — {t.title}
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="micro-label" style={{ margin: '26px 0 10px' }}>
          Propose a task
        </div>
        <div className="composer">
          <input
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void propose();
            }}
            placeholder="Task title — anyone can propose, only the assignee confirms"
          />
          <select
            value={newAssignee}
            onChange={(e) => setNewAssignee(e.target.value)}
            style={{
              border: '1px solid rgba(35,33,48,0.22)',
              borderRadius: 3,
              background: '#fff',
              fontSize: 12,
              fontFamily: 'inherit',
              color: 'var(--text-body)',
              padding: '0 8px',
            }}
          >
            <option value="">unassigned</option>
            {roster.map(({ profile }) => (
              <option key={profile.id} value={profile.id}>
                {firstNameOf(profile.display_name)}
              </option>
            ))}
          </select>
          <button className="btn-ink" onClick={() => void propose()}>
            PROPOSE
          </button>
        </div>

        <div style={{ fontSize: 11, color: 'var(--faint)', lineHeight: 1.6, padding: '14px 4px 0' }}>
          Anyone can propose a task for anyone — but no task is live until its assignee confirms it.
          GitHub activity adds weight where relevant. Quiet-member signals are never shown here;
          Comrade raises them privately.
        </div>
      </div>
    </main>
  );
}
