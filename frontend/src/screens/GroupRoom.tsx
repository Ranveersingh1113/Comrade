import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { supabase } from '../lib/supabase';
import {
  streamTurn, agentErrorText, getThreadRuns, rememberMessage, suppressObservation,
} from '../lib/agentApi';
import { activityLabel } from '../lib/toolActivity';
import { useIsNarrow } from '../hooks/useIsNarrow';
import { daysUntil, firstNameOf, messageTime, shortDate } from '../lib/format';
import { classifyMessage, memberBars } from '../lib/roomModel';
import type { ConsentItem, DocumentRow, MemoryCompilation, Message, Milestone, Task, Thread } from '../lib/types';
import { useTeam } from '../state/TeamContext';
import { useMessages } from '../hooks/useMessages';
import { useTeamRealtime } from '../hooks/useRealtime';
import { taskCell, taskMark, taskPill, useTasks, type TaskActions } from '../hooks/useTasks';
import { Avatar, AiOrb } from '../components/Avatar';
import { MemoryDiffCard } from '../components/MemoryDiffCard';
import { ComposerMode, type ComposerModeValue } from '../components/ComposerMode';
import { ConsentCard } from '../components/ConsentCard';
import { PreviewBar } from '../components/PreviewBar';
import { AgentActivity } from '../components/AgentActivity';
import type { AgentStep } from '../lib/agentApi';

type RoomLayout = 'classic' | 'split' | 'board';

export function GroupRoom({ thread }: { thread: Thread }) {
  const narrow = useIsNarrow();
  const { team, myUserId, profileOf } = useTeam();
  const { messages, compilationsByMessage, error, refresh } = useMessages(thread.id);
  const [consents, setConsents] = useState<ConsentItem[]>([]);
  const [consentError, setConsentError] = useState<string | null>(null);
  const taskState = useTasks();
  const [layout, setLayout] = useState<RoomLayout>(
    () => (localStorage.getItem('comrade.roomLayout') as RoomLayout | null) ?? 'classic',
  );
  const [draft, setDraft] = useState('');
  const [aiTyping, setAiTyping] = useState(false);
  const [pending, setPending] = useState('');
  const [sendError, setSendError] = useState<string | null>(null);
  const [step, setStep] = useState('');
  const [activity, setActivity] = useState<AgentStep[]>([]);
  const [agentNote, setAgentNote] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [composerMode, setComposerMode] = useState<ComposerModeValue>(() => {
    if (!thread) return 'team';
    return (localStorage.getItem(`comrade.composerMode.${myUserId}.${thread.id}`) as ComposerModeValue | null)
      ?? (thread.kind === 'work' ? 'agent' : 'team');
  });
  useEffect(() => {
    if (!thread) return;
    setComposerMode(
      (localStorage.getItem(`comrade.composerMode.${myUserId}.${thread.id}`) as ComposerModeValue | null)
        ?? (thread.kind === 'work' ? 'agent' : 'team'),
    );
  }, [myUserId, thread]);
  const [milestones, setMilestones] = useState<Milestone[]>([]);
  const [docs, setDocs] = useState<DocumentRow[]>([]);
  const chatRef = useRef<HTMLDivElement>(null);
  const activityRequest = useRef(0);

  const teamId = team?.id ?? '';

  useEffect(() => {
    if (!teamId) return;
    const request = ++activityRequest.current;
    setActivity([]);
    void getThreadRuns(teamId, thread.id).then((runs) => {
      if (request !== activityRequest.current) return;
      setActivity(runs.flatMap((run) => run.steps).filter(
        (item) => item.type === 'tool_call' || item.type === 'tool_result',
      ));
    }).catch(() => {
      // A historical activity read must not break a new live conversation.
    });
  }, [teamId, thread.id]);

  const refreshConsents = useCallback(async () => {
    if (!teamId) return;
    const { data, error: err } = await supabase
      .from('consent_queue')
      .select('*')
      .eq('team_id', teamId)
      .eq('thread_id', thread.id)
      .order('created_at');
    if (err) setConsentError(err.message);
    else {
      setConsents((data as ConsentItem[] | null) ?? []);
      setConsentError(null);
    }
  }, [teamId, thread.id]);

  useEffect(() => {
    void refreshConsents();
  }, [refreshConsents]);
  useTeamRealtime('consent_queue', teamId, refreshConsents, `thread_id=eq.${thread.id}`);

  const pickLayout = (l: RoomLayout) => {
    setLayout(l);
    localStorage.setItem('comrade.roomLayout', l);
  };

  useEffect(() => {
    if (!teamId) return;
    void supabase
      .from('milestones')
      .select('*')
      .eq('team_id', teamId)
      .order('due_at')
      .then(({ data }) => setMilestones((data as Milestone[] | null) ?? []));
    void supabase
      .from('documents')
      .select('*')
      .eq('team_id', teamId)
      .is('deleted_at', null)
      .order('created_at', { ascending: false })
      .limit(3)
      .then(({ data }) => setDocs((data as DocumentRow[] | null) ?? []));
  }, [teamId]);

  useEffect(() => {
    const el = chatRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length, consents.length, aiTyping]);

  const send = async () => {
    const text = draft.trim();
    if (!text || !teamId) return;
    setDraft('');
    setSendError(null);
    setNote(null);
    setAgentNote(null);
    const mentionsAi = composerMode === 'agent' || /@comrade/i.test(text);
    if (mentionsAi) {
      // Server persists both the user message and the AI reply; Realtime
      // (or the post-call refresh) delivers them — no optimistic insert.
      setAiTyping(true);
      setPending('');
      setStep('');
      activityRequest.current += 1;
      setActivity([]);
      try {
        await streamTurn(teamId, text, thread.id, (f) => {
          if (f.type === 'text') setPending((p) => p + (f.text ?? ''));
          // The runtime already says what it is doing; the room was throwing
          // it away and showing three dots instead.
          else if (f.type === 'tool_call' || f.type === 'tool_result') {
            if (f.type === 'tool_call') setStep(activityLabel(f.tool ?? ''));
            setActivity((steps) => [...steps, {
              type: f.type, seq: f.seq, tool: f.tool, args: f.args, response: f.response,
            }]);
          }
          // Q6. The room's turn lock is held by someone else's question, so
          // this turn never runs — and until D6 that arrived as nothing at
          // all: the indicator vanished, no reply appeared, and the member
          // had no way to tell it apart from a hang. Surfaced as a real
          // message, not an error, because nothing has gone wrong.
          else if (f.type === 'busy') setAgentNote(f.detail ?? null);
          // The model returned nothing. Same slot as 'busy' because it is the
          // same thing from the member's side — Comrade did not answer, and
          // here is why — and emphatically not an error banner: nothing they
          // did went wrong.
          else if (f.type === 'empty') setAgentNote(f.detail ?? null);
          // 🔴 And the same thing again, one layer down. Since the durable
          // queue the agent no longer runs inside this request: the browser
          // replays the run row, so the reason a turn produced nothing now
          // arrives on the TERMINAL frame instead of as 'empty'. Dropping it
          // put the blank screen back — indicator stops, no reply, no reason.
          //
          // Gated on the detail, not the status: this frame ends every turn,
          // including the ones that answered.
          else if (f.type === 'done' && f.detail) setAgentNote(f.detail);
          else if (f.type === 'error') setSendError(f.detail ?? 'Turn failed');
        });
      } catch (e) {
        setSendError(agentErrorText(e));
        setDraft(text);
      } finally {
        setAiTyping(false);
        setPending('');
        setStep('');
        await refresh();
      }
    } else {
      const { error: err } = await supabase.from('messages').insert({
        team_id: teamId,
        thread_id: thread.id,
        sender_kind: 'user',
        sender_id: myUserId,
        body: text,
      });
      if (err) {
        setSendError(err.message);
        setDraft(text);
      } else {
        await refresh();
      }
    }
  };

  const deleteForEveryone = async (m: Message) => {
    // Deletion leaves a trace: set deleted_scope, never remove the row.
    await supabase
      .from('messages')
      .update({
        deleted_scope: 'everyone',
        deleted_by: myUserId,
        deleted_at: new Date().toISOString(),
      })
      .eq('id', m.id);
    await refresh();
  };

  const suppressObs = async (m: Message) => {
    // "remove + don't do this again": the backend tombstones the message and
    // records a standing suppression the agent must respect.
    if (!team) return;
    try {
      await suppressObservation(m.id, team.id, 'proactive_observation');
    } catch (e) {
      setSendError(agentErrorText(e));
    }
    await refresh();
  };

  const remember = async (m: Message) => {
    // The compiler is the only writer of memory (§6.0), so this queues a
    // compile of that one message rather than inserting a fact. The member
    // gets told it was queued, not that it was remembered — the compiler
    // still decides whether there is a durable fact in there, and saying
    // otherwise would promise something this cannot deliver.
    if (!team) return;
    try {
      await rememberMessage(team.id, m.id);
      setNote('Queued — Comrade will fold that into the wiki and show the diff.');
    } catch (e) {
      setSendError(agentErrorText(e));
    }
  };

  const nextMilestone = milestones.find((m) => m.due_at && new Date(m.due_at) > new Date());
  const timeline = useMemo(
    () => [
      ...messages.map((item) => ({ kind: 'message' as const, item })),
      ...consents.map((item) => ({ kind: 'consent' as const, item })),
    ].sort((a, b) => a.item.created_at.localeCompare(b.item.created_at)),
    [messages, consents],
  );

  return (
    <main style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
      <header
        style={{
          flex: 'none',
          display: 'flex',
          alignItems: 'flex-end',
          gap: 16,
          padding: narrow ? '14px 16px 10px' : '20px 28px 14px',
          borderBottom: '1px solid var(--border-soft)',
        }}
      >
        <div>
          <div className="display" style={{ fontSize: 30 }}>
            {thread?.title ?? 'Group room'}
          </div>
          <div style={{ fontSize: 11, letterSpacing: '0.06em', color: 'var(--muted)', marginTop: 6 }}>
            {thread ? (thread.visibility === 'restricted' ? 'Selected members' : 'Team-visible') : 'Comrade reads everything · speaks only when spoken to'}
          </div>
        </div>
        <div
          style={{
            marginLeft: 'auto',
            display: 'flex',
            gap: 2,
            background: 'rgba(35,33,48,0.06)',
            borderRadius: 9,
            padding: 3,
          }}
        >
          {(['classic', 'split', 'board'] as const).map((l) => (
            <button
              key={l}
              onClick={() => pickLayout(l)}
              style={{
                border: 'none',
                cursor: 'pointer',
                fontSize: 11,
                fontWeight: 600,
                letterSpacing: '0.04em',
                borderRadius: 7,
                padding: '6px 13px',
                background: layout === l ? 'var(--ink)' : 'transparent',
                color: layout === l ? 'var(--paper)' : 'var(--muted)',
                textTransform: 'capitalize',
              }}
            >
              {l}
            </button>
          ))}
        </div>
      </header>

      {layout === 'board' && (
        <BoardStrip
          nextMilestone={nextMilestone}
          taskState={taskState}
          docs={docs}
        />
      )}

      {/* Narrow: stack the rail BELOW the conversation instead of beside it.
          Side by side, a 250px sidebar plus a 296px rail leaves 
          conversation on a 375px screen. */}
      <div
        style={{
          flex: 1,
          display: 'flex',
          minHeight: 0,
          flexDirection: narrow ? 'column' : 'row',
          overflowY: narrow ? 'auto' : 'hidden',
        }}
      >
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
          {/* A readable column, not the whole monitor. On a 1920px screen
              the transcript stretched edge to edge and every message read
              as one long line; a consent card became a metre of dark
              block. Centred with a cap, the thread reads like a
              conversation at any window size. */}
          <div
            ref={chatRef}
            style={{
              flex: 1, overflowY: 'auto', padding: '20px 0 8px',
              width: '100%', maxWidth: 1040, margin: '0 auto',
            }}
          >
            {(error || consentError) && (
              <div style={{ padding: '10px 28px', fontSize: 12, color: 'var(--terracotta)' }}>
                {error ?? consentError}
              </div>
            )}
            {timeline.map((entry) => entry.kind === 'consent' ? (
              <div key={entry.item.id} style={{ padding: '0 28px' }}>
                <ConsentCard item={entry.item} onResolved={refreshConsents} viewerId={myUserId} />
              </div>
            ) : (
              <MessageRow
                key={entry.item.id}
                m={entry.item}
                senderName={
                  entry.item.sender_kind === 'ai'
                    ? 'Comrade'
                    : (profileOf(entry.item.sender_id)?.display_name ?? 'Former member')
                }
                mine={entry.item.sender_id === myUserId}
                compilation={compilationsByMessage.get(entry.item.id) ?? null}
                onDelete={() => void deleteForEveryone(entry.item)}
                onSuppress={() => void suppressObs(entry.item)}
                onRemember={() => void remember(entry.item)}
              />
            ))}
            <AgentActivity steps={activity} />
            {aiTyping && (
              <div
                style={{
                  display: 'flex',
                  gap: 14,
                  padding: '10px 28px',
                  borderLeft: '3px solid var(--terracotta-soft)',
                }}
              >
                <span className="orb" style={{ width: 36, height: 36, fontSize: 13, animation: 'breathe 2s ease-in-out infinite' }}>
                  ◈
                </span>
                {pending ? (
                  <div
                    style={{
                      paddingTop: 10,
                      fontSize: 13.5,
                      lineHeight: 1.5,
                      color: 'var(--text-body)',
                      whiteSpace: 'pre-wrap',
                      overflowWrap: 'anywhere',
                    }}
                  >
                    {pending}
                  </div>
                ) : (
                  <div style={{ display: 'flex', gap: 9, alignItems: 'center', paddingTop: 13 }}>
                    {[0, 0.2, 0.4].map((d) => (
                      <span
                        key={d}
                        style={{
                          width: 6,
                          height: 6,
                          borderRadius: '50%',
                          background: 'var(--muted)',
                          animation: `blink 1.2s ${d}s infinite`,
                        }}
                      />
                    ))}
                    {step && activity.length === 0 && (
                      <span className="mono" style={{ fontSize: 10, color: 'var(--faint)' }}>
                        {step}
                      </span>
                    )}
                  </div>
                )}
              </div>
            )}
            {/* Comrade explaining why there is no reply — a busy room (Q6)
                or a model that returned nothing. In the transcript rather
                than beside the composer, because it IS Comrade answering, and
                not styled as an error, because the member did nothing wrong.
                Both causes share the slot: from where they sit, the question
                is the same one. */}
            {agentNote && (
              <div
                data-agent-note
                style={{
                  display: 'flex',
                  gap: 14,
                  padding: '10px 28px',
                  borderLeft: '3px solid var(--border-soft)',
                }}
              >
                <span className="orb" style={{ width: 36, height: 36, fontSize: 13, opacity: 0.55 }}>
                  ◈
                </span>
                <div
                  style={{
                    paddingTop: 10,
                    fontSize: 13,
                    lineHeight: 1.5,
                    color: 'var(--muted)',
                  }}
                >
                  {agentNote}
                </div>
              </div>
            )}
          </div>
          <div style={{ flex: 'none', padding: '12px 28px 18px' }}>
            {sendError && (
              <div style={{ fontSize: 12, color: 'var(--terracotta)', marginBottom: 8 }}>
                {sendError}
              </div>
            )}
            {/* Confirmations, not errors — muted, and cleared by the next send. */}
            {note && (
              <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 8 }}>
                {note}
              </div>
            )}
            {/* What is running in this thread, directly above the
                composer: a preview is a thing you go and look at, and it
                belongs beside the place you type rather than buried in
                the transcript where it scrolls away. */}
            <PreviewBar teamId={teamId} threadId={thread.id} />
            <div className="composer">
              {thread && <ComposerMode userId={myUserId} threadId={thread.id} defaultMode={thread.kind === 'work' ? 'agent' : 'team'} onChange={setComposerMode} />}
              <input
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void send();
                }}
                placeholder={composerMode === 'agent' ? 'Ask Comrade…' : 'Message the team… @Comrade to ask the AI'}
              />
              <button className="btn-ink" onClick={() => void send()}>
                SEND
              </button>
            </div>
          </div>
        </div>

        {layout === 'classic' && (
          <ClassicPanel narrow={narrow} nextMilestone={nextMilestone} milestones={milestones} taskState={taskState} docs={docs} />
        )}
        {layout === 'split' && <SplitPanel taskState={taskState} narrow={narrow} />}
      </div>
    </main>
  );
}

/* ---------------- message row ---------------- */

function MessageRow({
  m,
  senderName,
  mine,
  compilation,
  onDelete,
  onSuppress,
  onRemember,
}: {
  m: Message;
  senderName: string;
  mine: boolean;
  compilation: MemoryCompilation | null;
  onDelete: () => void;
  onSuppress: () => void;
  onRemember: () => void;
}) {
  const cls = classifyMessage(
    m,
    compilation?.diff_message_id ? new Set([compilation.diff_message_id]) : new Set(),
  );
  const isAI = cls.kind === 'ai';
  const [hover, setHover] = useState(false);

  if (cls.kind === 'deleted') {
    return (
      <div className="fade-up" style={{ display: 'flex', gap: 14, padding: '10px 28px' }}>
        <span
          style={{
            display: 'flex',
            width: 36,
            height: 36,
            flex: 'none',
            borderRadius: '50%',
            border: '1.5px dashed rgba(35,33,48,0.25)',
            color: 'var(--faint)',
            fontSize: 13,
            alignItems: 'center',
            justifyContent: 'center',
            marginTop: 2,
          }}
        >
          ◌
        </span>
        <div style={{ fontSize: 12.5, fontStyle: 'italic', color: 'var(--faint)', paddingTop: 10 }}>
          {senderName} removed a message — removed for everyone ·{' '}
          <span className="mono" style={{ fontSize: 10, fontStyle: 'normal' }}>
            {messageTime(m.created_at)}
          </span>
        </div>
      </div>
    );
  }

  return (
    <div
      className="fade-up"
      data-sender={m.sender_kind}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        display: 'flex',
        gap: 14,
        padding: '10px 28px',
        borderLeft: `3px solid ${isAI ? 'var(--terracotta-soft)' : 'transparent'}`,
        background: isAI ? 'rgba(228,121,91,0.05)' : 'transparent',
      }}
    >
      {isAI ? (
        <AiOrb size={36} />
      ) : (
        <Avatar userId={m.sender_id ?? 'unknown'} name={senderName} size={36} />
      )}
      <div style={{ minWidth: 0, flex: 1 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 9 }}>
          <span style={{ fontSize: 13.5, fontWeight: 700, letterSpacing: '-0.01em' }}>
            {senderName}
          </span>
          {isAI && (
            <span
              style={{
                fontSize: 8.5,
                fontWeight: 700,
                letterSpacing: '0.14em',
                color: 'var(--terracotta)',
                border: '1px solid rgba(210,89,59,0.4)',
                borderRadius: 3,
                padding: '2px 6px',
              }}
            >
              AI · SEEN BY ALL
            </span>
          )}
          {/* Provenance, beside the member's own name rather than instead
              of it: they wrote it and they stand behind it (§13.4). */}
          {m.ai_assisted && (
            <span
              className="mono"
              style={{
                fontSize: 9,
                letterSpacing: '0.1em',
                textTransform: 'uppercase',
                color: 'var(--muted)',
                border: '1px solid var(--border-soft)',
                borderRadius: 2,
                padding: '2px 6px',
              }}
            >
              drafted with Comrade
            </span>
          )}
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--faint)' }}>
            {messageTime(m.created_at)}
          </span>
          {mine && hover && !isAI && (
            <button
              onClick={onDelete}
              className="mono"
              title="Remove for everyone (leaves a visible placeholder)"
              style={{
                border: 'none',
                background: 'transparent',
                color: 'var(--faint)',
                fontSize: 10,
                cursor: 'pointer',
              }}
            >
              ✕ remove
            </button>
          )}
          {/* Any member may mark any human message as worth keeping
              (findings §20.7.1). Not restricted to your own: the point is that
              a HUMAN judged it durable, and noticing that a teammate said
              something important is the same signal as saying it yourself.
              AI messages are excluded — the agent's own output is not a
              source, and compiling it would let memory cite itself. */}
          {!isAI && hover && (
            <button
              onClick={onRemember}
              className="mono"
              title="Queue this for the wiki — Comrade compiles it, cites it, and you can revert it"
              style={{
                border: 'none',
                background: 'transparent',
                color: 'var(--faint)',
                fontSize: 10,
                cursor: 'pointer',
              }}
            >
              ✦ remember this
            </button>
          )}
          {/* Proactive AI observations get a one-tap standing objection (any
              member; diff cards are notifications, not observations). */}
          {isAI && !compilation && hover && (
            <button
              onClick={onSuppress}
              className="mono"
              title="Tombstones this message and tells Comrade not to post this kind of observation again"
              style={{
                border: '1px solid rgba(210,89,59,0.35)',
                background: 'transparent',
                color: 'var(--terracotta)',
                fontSize: 9,
                letterSpacing: '0.1em',
                borderRadius: 2,
                padding: '2px 7px',
                cursor: 'pointer',
              }}
            >
              ✕ REMOVE · DON'T DO THIS AGAIN
            </button>
          )}
        </div>
        <div
          style={{
            fontSize: 13.5,
            lineHeight: 1.5,
            color: 'var(--text-body)',
            marginTop: 3,
            whiteSpace: 'pre-wrap',
            overflowWrap: 'anywhere',
          }}
        >
          {m.body}
        </div>
        {compilation && <MemoryDiffCard compilation={compilation} />}
      </div>
    </div>
  );
}

/* ---------------- contribution bars (shared by classic/board) ---------------- */

function useMemberBars(taskState: TaskActions) {
  const { roster } = useTeam();
  return useMemo(
    () =>
      memberBars(roster, taskState.tasks).map((b) => ({
        id: b.userId,
        first: firstNameOf(b.name),
        tasks: b.tasks,
        label: `${b.doneCount}/${b.tasks.length} done`,
      })),
    [roster, taskState.tasks],
  );
}

function TaskCells({ tasks, size = 11 }: { tasks: Task[]; size?: number }) {
  return (
    <span style={{ display: 'flex', gap: 3 }}>
      {tasks.map((t) => {
        const c = taskCell(t.status);
        return (
          <span
            key={t.id}
            title={`${t.title} — ${t.status}`}
            style={{
              width: size,
              height: size,
              borderRadius: 2,
              background: c.bg,
              border: `1px ${c.style} ${c.border}`,
            }}
          />
        );
      })}
    </span>
  );
}

/* ---------------- classic right panel ---------------- */

function ClassicPanel({
  narrow,
  nextMilestone,
  milestones,
  taskState,
  docs,
}: {
  narrow: boolean;
  nextMilestone: Milestone | undefined;
  milestones: Milestone[];
  taskState: TaskActions;
  docs: DocumentRow[];
}) {
  const bars = useMemberBars(taskState);
  const later = milestones.filter((m) => m.id !== nextMilestone?.id && m.due_at);
  return (
    <aside
      style={{
        width: narrow ? '100%' : 296,
        flex: 'none',
        borderLeft: '1px solid var(--border-soft)',
        overflowY: 'auto',
        padding: '22px 22px 26px',
        display: 'flex',
        flexDirection: 'column',
        gap: 26,
      }}
    >
      <div>
        <div className="micro-label" style={{ marginBottom: 12 }}>
          Next deadline
        </div>
        {nextMilestone?.due_at ? (
          <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
            <span className="display" style={{ fontSize: 74, lineHeight: 0.9, color: 'var(--plum)' }}>
              {daysUntil(nextMilestone.due_at)}
            </span>
            <span style={{ fontSize: 12, lineHeight: 1.6, color: 'var(--text-soft)' }}>
              days until
              <br />
              <b style={{ color: 'var(--text)', fontSize: 13 }}>{nextMilestone.title}</b>
              <br />
              <span className="mono" style={{ fontSize: 10.5, color: 'var(--muted)' }}>
                {shortDate(nextMilestone.due_at)}
              </span>
            </span>
          </div>
        ) : (
          <div style={{ fontSize: 12, color: 'var(--faint)' }}>
            No upcoming milestone — add one in Tasks.
          </div>
        )}
        {later.length > 0 && (
          <div
            style={{
              marginTop: 14,
              paddingTop: 12,
              borderTop: '1px dashed rgba(35,33,48,0.18)',
              display: 'flex',
              flexDirection: 'column',
              gap: 6,
            }}
          >
            {later.slice(0, 3).map((m) => (
              <div key={m.id} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12 }}>
                <span style={{ color: 'var(--text-soft)' }}>{m.title}</span>
                <span className="mono" style={{ fontSize: 10.5, color: 'var(--muted)' }}>
                  {m.due_at ? shortDate(m.due_at) : ''}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      <div>
        <div className="micro-label" style={{ marginBottom: 12 }}>
          Contribution
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 11 }}>
          {bars.map((b) => (
            <div key={b.id} style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <span style={{ width: 54, flex: 'none', fontSize: 12, color: 'var(--text-soft)' }}>
                {b.first}
              </span>
              <span style={{ flex: 1 }}>
                <TaskCells tasks={b.tasks} />
              </span>
              <span className="mono" style={{ fontSize: 10.5, color: 'var(--muted)' }}>
                {b.label}
              </span>
            </div>
          ))}
        </div>
        <div style={{ fontSize: 10.5, color: 'var(--faint)', marginTop: 11, lineHeight: 1.5 }}>
          One cell per task — filled when done. No rankings; quiet-member signals go to private
          threads only.
        </div>
      </div>

      <div>
        <div className="micro-label" style={{ marginBottom: 12 }}>
          Recent docs
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 9, fontSize: 12.5 }}>
          {docs.length === 0 && (
            <span style={{ color: 'var(--faint)', fontSize: 12 }}>Nothing shared yet.</span>
          )}
          {docs.map((d) => (
            <div key={d.id} style={{ display: 'flex', gap: 9 }}>
              <span>▤</span>
              <span>
                {d.filename ?? d.kind}
                <span
                  style={{ display: 'block', fontSize: 10.5, color: 'var(--muted)', marginTop: 1 }}
                >
                  {d.status === 'ready' ? 'in wiki' : d.status}
                </span>
              </span>
            </div>
          ))}
        </div>
      </div>
    </aside>
  );
}

/* ---------------- split layout panel ---------------- */

function SplitPanel({ taskState, narrow }: { taskState: TaskActions; narrow: boolean }) {
  const { roster, myUserId } = useTeam();
  return (
    <aside
      style={{
        width: narrow ? '100%' : 364,
        flex: 'none',
        borderLeft: '1px solid var(--border-soft)',
        overflowY: 'auto',
        padding: 22,
      }}
    >
      <div className="micro-label" style={{ marginBottom: 14 }}>
        Tasks
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
        {roster.map(({ profile }) => {
          const mine = taskState.tasks.filter((t) => t.assignee_id === profile.id);
          if (mine.length === 0) return null;
          return (
            <div key={profile.id}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginBottom: 8 }}>
                <Avatar userId={profile.id} name={profile.display_name} size={22} />
                <span style={{ fontSize: 13, fontWeight: 700 }}>
                  {firstNameOf(profile.display_name)}
                </span>
                <span style={{ marginLeft: 'auto' }}>
                  <TaskCells tasks={mine} size={10} />
                </span>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                {mine.map((t) => {
                  const c = taskCell(t.status);
                  const p = taskPill(t.status);
                  const isMine = t.assignee_id === myUserId;
                  const canAdvance = isMine && t.status !== 'done';
                  return (
                    <button
                      key={t.id}
                      onClick={() => canAdvance && void taskState.advance(t)}
                      disabled={!canAdvance}
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 8,
                        border: 'none',
                        background: 'transparent',
                        padding: '3px 0 3px 31px',
                        fontSize: 12.5,
                        color: t.status === 'done' ? 'var(--faint)' : 'var(--text-body)',
                        cursor: canAdvance ? 'pointer' : 'default',
                        textAlign: 'left',
                      }}
                    >
                      <span
                        style={{
                          width: 14,
                          height: 14,
                          flex: 'none',
                          borderRadius: 3,
                          border: `1.5px ${c.style} ${c.border}`,
                          background: c.bg,
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
                          textDecoration: t.status === 'done' ? 'line-through' : 'none',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                        }}
                      >
                        {t.title}
                      </span>
                      <span
                        className="mono"
                        style={{
                          marginLeft: 'auto',
                          fontSize: 8.5,
                          letterSpacing: '0.1em',
                          color: p.color,
                          flex: 'none',
                        }}
                      >
                        {p.label}
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
    </aside>
  );
}

/* ---------------- board strip ---------------- */

function BoardStrip({
  nextMilestone,
  taskState,
  docs,
}: {
  nextMilestone: Milestone | undefined;
  taskState: TaskActions;
  docs: DocumentRow[];
}) {
  const bars = useMemberBars(taskState);
  return (
    <div
      style={{
        flex: 'none',
        display: 'flex',
        gap: 0,
        padding: '0 28px',
        borderBottom: '1px solid var(--border-soft)',
      }}
    >
      <div
        style={{
          flex: 1,
          padding: '16px 20px 16px 0',
          borderRight: '1px solid rgba(35,33,48,0.1)',
          display: 'flex',
          alignItems: 'center',
          gap: 14,
        }}
      >
        {nextMilestone?.due_at ? (
          <>
            <span className="display" style={{ fontSize: 52, color: 'var(--plum)' }}>
              {daysUntil(nextMilestone.due_at)}
            </span>
            <span
              style={{
                fontSize: 11,
                letterSpacing: '0.1em',
                textTransform: 'uppercase',
                color: 'var(--muted)',
                lineHeight: 1.6,
              }}
            >
              days until
              <br />
              <b style={{ color: 'var(--text)' }}>{nextMilestone.title}</b>
            </span>
          </>
        ) : (
          <span style={{ fontSize: 12, color: 'var(--faint)' }}>No upcoming milestone</span>
        )}
      </div>
      <div style={{ flex: 1.4, padding: '16px 20px', borderRight: '1px solid rgba(35,33,48,0.1)' }}>
        <div className="micro-label" style={{ marginBottom: 9 }}>
          Contribution
        </div>
        <div style={{ display: 'flex', gap: 18, flexWrap: 'wrap' }}>
          {bars.map((b) => (
            <div key={b.id}>
              <div style={{ fontSize: 11, color: 'var(--text-soft)', marginBottom: 5 }}>
                {b.first}
              </div>
              <TaskCells tasks={b.tasks} size={9} />
            </div>
          ))}
        </div>
      </div>
      <div style={{ flex: 1, padding: '16px 0 16px 20px', fontSize: 12, lineHeight: 1.7 }}>
        <div className="micro-label" style={{ marginBottom: 9 }}>
          Docs
        </div>
        {docs.slice(0, 2).map((d) => (
          <div key={d.id}>▤ {d.filename ?? d.kind}</div>
        ))}
        {docs.length === 0 && <span style={{ color: 'var(--faint)' }}>none yet</span>}
      </div>
    </div>
  );
}
