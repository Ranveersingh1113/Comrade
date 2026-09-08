import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { supabase } from '../lib/supabase';
import {
  startTurn, followRun, cancelRun, ACTIVE_RUN_STATUSES, agentErrorText,
  AgentApiError, failureReference,
  getThreadRuns, rememberMessage, suppressObservation,
} from '../lib/agentApi';
import type { StreamFrame } from '../lib/agentApi';
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
import { ThreadRoster } from '../components/ThreadRoster';
import { AgentActivity } from '../components/AgentActivity';
import type { AgentStep } from '../lib/agentApi';

type RoomLayout = 'classic' | 'split' | 'board';

/** Is this worth reconnecting for?
 *
 *  🔴 (fix.md F10) Nothing asked. `followRun` returns `aborted` when the
 *  signal fired and `truncated` when the body ended cleanly, so everything
 *  that reached the catch arm was the connection actually DYING — a TypeError
 *  out of fetch or the body reader, no status, no frame. That is the
 *  commonest interruption there is, and it was the only one that never
 *  retried: the indicator went out, "No connection to Comrade" appeared, and
 *  the run carried on answering nobody.
 *
 *  A server ANSWER is different. 401/403/404 are decisions — the session
 *  expired, access was revoked, the run is not there — and reconnecting
 *  cannot change any of them. 5xx is worth another try.
 */
function reconnectable(e: unknown): boolean {
  return !(e instanceof AgentApiError) || e.status >= 500;
}

/** How long to wait before each reconnect. Short, because someone is watching
 *  a live answer arrive; growing, because a network that has just failed
 *  usually needs more than no time at all. */
const RECONNECT_MS = [200, 400, 800, 1600];

export function GroupRoom({ thread, allowTeamMessages = true }: { thread: Thread; allowTeamMessages?: boolean }) {
  const narrow = useIsNarrow();
  const { team, myUserId, profileOf } = useTeam();
  const {
    messages, compilationsByMessage, error, refresh,
    hasOlder, loadingOlder, loadOlder, live,
  } = useMessages(thread.id);
  const [consents, setConsents] = useState<ConsentItem[]>([]);
  const [consentError, setConsentError] = useState<string | null>(null);
  const taskState = useTasks();
  const [layout, setLayout] = useState<RoomLayout>(
    () => (localStorage.getItem('comrade.roomLayout') as RoomLayout | null) ?? 'classic',
  );
  // 🔴 The draft lived in component state alone, so switching threads to
  // check something threw away whatever had been typed. Kept per thread AND
  // per member: two people at one machine must not inherit each other's
  // half-written messages.
  const draftKey = `comrade.draft.${myUserId}.${thread.id}`;
  const [draft, setDraftState] = useState(
    () => localStorage.getItem(draftKey) ?? '',
  );
  const setDraft = useCallback((next: string) => {
    setDraftState(next);
    try {
      if (next) localStorage.setItem(draftKey, next);
      else localStorage.removeItem(draftKey);
    } catch {
      // A browser refusing storage must not stop somebody typing.
    }
  }, [draftKey]);
  useEffect(() => {
    setDraftState(localStorage.getItem(draftKey) ?? '');
  }, [draftKey]);
  /** In flight, so a second press is not a second question. */
  const [sending, setSending] = useState(false);
  const [aiTyping, setAiTyping] = useState(false);
  const [pending, setPending] = useState('');
  const [sendError, setSendError] = useState<string | null>(null);
  const [step, setStep] = useState('');
  const [activity, setActivity] = useState<AgentStep[]>([]);
  const [agentNote, setAgentNote] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  // A work thread is mostly Comrade's, so it opens on Comrade — but the
  // switch stays, because the people in it still need to talk to each other
  // about the work. A remembered choice wins over both.
  const defaultMode: ComposerModeValue = thread.kind === 'work' ? 'agent' : 'team';
  const [composerMode, setComposerMode] = useState<ComposerModeValue>(() => {
    if (!allowTeamMessages) return 'agent';
    return (localStorage.getItem(`comrade.composerMode.${myUserId}.${thread.id}`) as ComposerModeValue | null)
      ?? defaultMode;
  });
  useEffect(() => {
    if (!allowTeamMessages) {
      setComposerMode('agent');
      return;
    }
    setComposerMode(
      (localStorage.getItem(`comrade.composerMode.${myUserId}.${thread.id}`) as ComposerModeValue | null)
        ?? defaultMode,
    );
  }, [myUserId, thread, allowTeamMessages, defaultMode]);
  const [milestones, setMilestones] = useState<Milestone[]>([]);
  const [docs, setDocs] = useState<DocumentRow[]>([]);
  const chatRef = useRef<HTMLDivElement>(null);
  const activityRequest = useRef(0);

  const teamId = team?.id ?? '';

  /** The GET stream currently being followed, so navigation can drop it.
   *
   *  Aborting this cancels the SUBSCRIPTION and nothing else. The run is
   *  durable and keeps working; leaving a room must not quietly kill a turn a
   *  teammate is waiting on. */
  const followRef = useRef<AbortController | null>(null);
  /** The run that watch is FOR.
   *
   *  A second follow of the same run reattaches at `after_seq=-1` and replays
   *  every step card already on screen. That was theoretical until the
   *  composer was released mid-run (fix.md F09): a correction typed during a
   *  run is steering, and the server answers steering with the id of the run
   *  already in flight. */
  const followingRef = useRef<string | null>(null);
  /** The attempt id of a send not yet confirmed accepted.
   *
   *  Retained across a retry of the SAME text, which is what makes the retry
   *  safe: the server recognises it and returns the run already accepted
   *  instead of posting the question a second time. */
  const attemptRef = useRef<{ text: string; id: string } | null>(null);
  /** The run being watched, and whether this member is the one who asked for
   *  it. Seeing a teammate's turn is not standing for them, so only its
   *  requester is offered the stop. */
  const [activeRun, setActiveRun] = useState<{ id: string; mine: boolean } | null>(null);
  const [stopping, setStopping] = useState(false);

  const attemptId = (text: string) => {
    if (attemptRef.current?.text === text) return attemptRef.current.id;
    const id = globalThis.crypto?.randomUUID?.()
      ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    attemptRef.current = { text, id };
    return id;
  };

  const handleFrame = useCallback((f: StreamFrame) => {
    if (f.type === 'text') setPending((prev) => prev + (f.text ?? ''));
    // The runtime already says what it is doing; the room was throwing it
    // away and showing three dots instead.
    else if (f.type === 'tool_call' || f.type === 'tool_result') {
      if (f.type === 'tool_call') setStep(activityLabel(f.tool ?? ''));
      setActivity((steps) => [...steps, {
        type: f.type, seq: f.seq, tool: f.tool, args: f.args, response: f.response,
      }]);
    }
    // Lifecycle, kept apart from content. 'queued' means no worker has picked
    // this up yet, which is a different wait from a model that is thinking,
    // and the room can now say which one the member is looking at.
    else if (f.type === 'run' || f.type === 'status') {
      if (f.status === 'queued') setStep('waiting for a worker');
      else if (f.status && f.status.startsWith('waiting_')) {
        setStep('');
        setAgentNote(f.detail ?? 'Waiting for your decision above.');
      }
    }
    // Q6. The room's turn lock is held by someone else's question, so this
    // turn never runs. Surfaced as a real message, not an error, because
    // nothing has gone wrong.
    else if (f.type === 'busy') setAgentNote(f.detail ?? null);
    // The model returned nothing. Same slot as 'busy' because it is the same
    // thing from the member's side: Comrade did not answer, and here is why.
    else if (f.type === 'empty') setAgentNote(f.detail ?? null);
    // Gated on the detail, not the status: this frame ends every turn,
    // including the ones that answered.
    else if (f.type === 'done' && f.detail) setAgentNote(f.detail);
    else if (f.type === 'error') setSendError(f.detail ?? 'Turn failed');
  }, []);

  /** Watch a durable run to its end, reattaching across dropped connections.
   *
   *  There was no such thing before: the browser watched a run only through
   *  the POST that started it, so a refresh or a severed connection ended the
   *  watch for good. The indicator stopped, no reply appeared, and the member
   *  could not tell a finished turn from an abandoned one, while the run
   *  itself carried on perfectly well, leased and durable, answering nobody. */
  const follow = useCallback(async (runId: string, mine = true, fromSeq = -1) => {
    // Already on it. Every caller can now ask for a run that may already be
    // watched — a steering send, the mount-time resume, a resolved consent
    // card — and restarting the stream would duplicate what is on screen.
    if (followingRef.current === runId) return;
    followRef.current?.abort();
    const control = new AbortController();
    followRef.current = control;
    followingRef.current = runId;
    setActiveRun({ id: runId, mine });
    setAiTyping(true);
    let cursor = fromSeq;
    let lost = false;
    // Bounded. A server that keeps cutting the stream is a thing to report,
    // not a thing to hammer.
    for (let attempt = 0; attempt < 5; attempt += 1) {
      let outcome;
      try {
        outcome = await followRun(teamId, runId, (f) => {
          if (typeof f.seq === 'number') cursor = Math.max(cursor, f.seq);
          handleFrame(f);
        }, { afterSeq: cursor, signal: control.signal });
      } catch (e) {
        // 🔴 This was `setSendError(...); break` — every throw ended the
        // watch. See `reconnectable` above: the throws are the dropped
        // connections, which are exactly the ones worth resuming.
        if (control.signal.aborted) return;
        if (!reconnectable(e)) {
          setSendError(agentErrorText(e));
          break;
        }
        // Logged even though it is being retried: a turn that reconnected
        // four times still had four failures worth knowing about.
        failureReference(e);
        if (attempt === 4) {
          lost = true;
          break;
        }
        await new Promise((r) => { setTimeout(r, RECONNECT_MS[attempt]); });
        // The wait is long enough for the member to have left the room.
        if (control.signal.aborted) return;
        // `cursor` already holds the last sequence that arrived, so the
        // reconnect asks for what follows it rather than the whole run.
        continue;
      }
      // Navigated away. Leave every piece of state alone: this room is going.
      if (outcome === 'aborted') return;
      if (outcome !== 'truncated') break;
      if (attempt === 4) lost = true;
    }
    if (lost) {
      // NOT "the turn failed". The run is leased and durable and is most
      // likely still working; what was lost is this browser's view of it.
      setSendError(
        'Lost the connection to this turn - it may still be running.'
        + ' Reload to catch up.',
      );
    }
    if (control.signal.aborted) return;
    followRef.current = null;
    followingRef.current = null;
    setActiveRun(null);
    setStopping(false);
    setAiTyping(false);
    setPending('');
    setStep('');
    await refresh();
  }, [teamId, handleFrame, refresh]);

  const stop = useCallback(async () => {
    if (!activeRun || !teamId) return;
    setStopping(true);
    try {
      await cancelRun(teamId, activeRun.id);
      // Nothing else to do here. The run row changes, and the stream this
      // room is already following reports the final state on its next poll —
      // one source of truth for how a turn ended rather than two.
    } catch (e) {
      setStopping(false);
      setSendError(agentErrorText(e));
    }
  }, [activeRun, teamId]);

  // Dropping the subscription is the ONLY thing leaving a thread does.
  useEffect(() => () => {
    followRef.current?.abort();
    followingRef.current = null;
  }, [thread.id]);

  useEffect(() => {
    if (!teamId) return;
    const request = ++activityRequest.current;
    setActivity([]);
    void getThreadRuns(teamId, thread.id).then((runs) => {
      if (request !== activityRequest.current) return;
      setActivity(runs.flatMap((run) => run.steps).filter(
        (item) => item.type === 'tool_call' || item.type === 'tool_result',
      ));
      // Reconstructed from server history, not from anything the browser
      // kept. A refresh mid-turn used to land on a silent room; it now
      // rejoins the run that is still going.
      const live = runs.find((run) => ACTIVE_RUN_STATUSES.has(run.status ?? ''));
      if (live) void follow(live.id, live.requester_id === myUserId);
    }).catch(() => {
      // A historical activity read must not break a new live conversation.
    });
    // myUserId is a dependency because it decides OWNERSHIP: the same run is
    // stoppable by one viewer and not another, and this room re-renders under
    // a different member without remounting.
  }, [teamId, thread.id, follow, myUserId]);

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

  // 🔴 This used to force the view to the bottom on EVERY change, which fights
  // anyone reading history: scroll up, a message arrives, and you are yanked
  // back down mid-sentence. Follow only when already near the bottom, and
  // offer a way back rather than deciding for them.
  const NEAR_BOTTOM_PX = 120;
  const [pinned, setPinned] = useState(true);
  const [unread, setUnread] = useState(0);
  const lastCount = useRef(0);

  const onScroll = useCallback(() => {
    const el = chatRef.current;
    if (!el) return;
    const atBottom =
      el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_PX;
    setPinned(atBottom);
    if (atBottom) setUnread(0);
    // Prepending older history while the view sits at the top would otherwise
    // load page after page in one gesture.
    if (el.scrollTop < 200 && hasOlder && !loadingOlder) {
      const before = el.scrollHeight;
      void loadOlder().then(() => {
        // Keep the reader where they were. Prepending grows the document
        // upward, so without this correction the content jumps by exactly the
        // height of what was just added.
        const after = chatRef.current;
        if (after) after.scrollTop += after.scrollHeight - before;
      });
    }
  }, [hasOlder, loadingOlder, loadOlder]);

  useEffect(() => {
    const el = chatRef.current;
    if (!el) return;
    const grew = messages.length > lastCount.current;
    lastCount.current = messages.length;
    if (pinned) {
      el.scrollTop = el.scrollHeight;
      setUnread(0);
    } else if (grew) {
      setUnread((n) => n + 1);
    }
  }, [messages.length, consents.length, aiTyping, pinned]);

  const send = async () => {
    const text = draft.trim();
    // 🔴 Nothing stopped a second send. A double click, or an impatient press
    // while the first was still in flight, asked the question twice — and the
    // attempt id makes a RETRY safe, not a second deliberate press.
    if (!text || !teamId || sending) return;
    setSending(true);
    setDraft('');
    setSendError(null);
    setNote(null);
    setAgentNote(null);
    const mentionsAi = !allowTeamMessages || composerMode === 'agent' || /@comrade/i.test(text);
    try {
      await deliver(text, mentionsAi);
    } finally {
      setSending(false);
    }
  };

  /** The send itself, so the in-flight flag has exactly one place to clear. */
  const deliver = async (text: string, mentionsAi: boolean) => {
    if (mentionsAi) {
      // Server persists both the user message and the AI reply; Realtime
      // (or the post-call refresh) delivers them — no optimistic insert.
      setAiTyping(true);
      const requestId = attemptId(text);
      let accepted;
      try {
        accepted = await startTurn(teamId, text, thread.id, requestId);
      } catch (e) {
        // The turn may or may not have been accepted - that is exactly what
        // this failure cannot tell us. Putting the text back is safe only
        // because the retry carries the SAME attempt id, so a turn that did
        // land comes back as a duplicate instead of being asked twice.
        setAiTyping(false);
        setSendError(agentErrorText(e));
        setDraft(text);
        return;
      }
      // Accepted. The next send is a new attempt, even with identical text.
      attemptRef.current = null;
      // 🔴 This was awaited, and `sending` was held until it returned — so the
      // composer was disabled for the WHOLE run, minutes on a tool-using turn.
      // The person most likely to need to speak during a run is the one who
      // started it ("not that file", "stop after the tests"), and the backend
      // has accepted exactly that as steering since T15. The only way to type
      // again was a reload, which also looks like abandoning the turn.
      //
      // The guard belongs on admission — the window a double click lands in —
      // not on the stream's lifetime, so the follow runs on its own.
      if (followingRef.current === accepted.run_id) return;
      // A NEW run, so the tool activity of the previous one is history. Reset
      // here rather than before the POST: steering shares the run in flight,
      // and clearing its activity would erase the turn the member is watching.
      setPending('');
      setStep('');
      activityRequest.current += 1;
      setActivity([]);
      void follow(accepted.run_id, true);
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
    //
    // 🔴 The result was awaited and then ignored. RLS refusing this produced
    // no error and no message, and the refresh underneath put the message
    // straight back — so a refused delete was indistinguishable from a UI
    // that had not noticed the click.
    const { error: err } = await supabase
      .from('messages')
      .update({
        deleted_scope: 'everyone',
        deleted_by: myUserId,
        deleted_at: new Date().toISOString(),
      })
      .eq('id', m.id);
    if (err) {
      setSendError(`That message could not be deleted: ${err.message}`);
      return;
    }
    setSendError(null);
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
    <main className="group-room" style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
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
            onScroll={onScroll}
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
                <ConsentCard
                  item={entry.item}
                  onResolved={() => {
                    void refreshConsents();
                    // Approve, edit or reject — all three resume the same
                    // logical run, and until this the room did not rejoin it.
                    // The member answered and then watched nothing happen.
                    if (entry.item.agent_run_id) {
                      void follow(entry.item.agent_run_id,
                        entry.item.requesting_member_id === myUserId);
                    }
                  }}
                  viewerId={myUserId}
                />
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
                data-agent-typing
                style={{
                  position: 'relative',
                  display: 'flex',
                  gap: 14,
                  padding: '10px 28px',
                  borderLeft: '3px solid var(--terracotta-soft)',
                }}
              >
                <AiOrb size={36} breathing />
                {activeRun?.mine && (
                  <button
                    type="button"
                    onClick={() => void stop()}
                    disabled={stopping}
                    className="mono"
                    style={{
                      position: 'absolute', right: 28,
                      border: '1px solid var(--border-soft)', borderRadius: 6,
                      background: 'transparent', color: 'var(--muted)',
                      fontSize: 10, letterSpacing: '0.08em', padding: '4px 9px',
                      cursor: stopping ? 'default' : 'pointer',
                    }}
                  >
                    {stopping ? 'STOPPING' : 'STOP'}
                  </button>
                )}
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
            {/* A dropped websocket used to freeze the room silently: no new
                messages, no explanation, and nothing to do but wait. It
                reconnects and refetches on its own — this only says why the
                room went quiet in the meantime. */}
            {!live && (
              <div
                data-realtime-offline
                className="mono"
                style={{
                  margin: '6px 28px',
                  padding: '6px 10px',
                  border: '1px solid var(--border-soft)',
                  borderRadius: 6,
                  fontSize: 10,
                  letterSpacing: '0.06em',
                  color: 'var(--muted)',
                }}
              >
                RECONNECTING — NEW MESSAGES MAY BE DELAYED
              </div>
            )}
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
                <AiOrb size={36} style={{ opacity: 0.55 }} />
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
            {unread > 0 && !pinned && (
              <button
                className="btn-ghost"
                onClick={() => {
                  const el = chatRef.current;
                  if (el) el.scrollTop = el.scrollHeight;
                  setPinned(true);
                  setUnread(0);
                }}
                style={{
                  alignSelf: 'center', marginBottom: 6, fontSize: 11,
                  padding: '3px 12px',
                }}
              >
                {unread} NEW {unread === 1 ? 'MESSAGE' : 'MESSAGES'} ↓
              </button>
            )}
            <PreviewBar teamId={teamId} threadId={thread.id} />
            {/* Only for a thread whose audience is a choice somebody made. A
                team-visible thread's roster is "the team", and a control for
                that would do nothing. In the room rather than a side panel,
                because the room has three layouts and two of them have no
                side panel at all. */}
            {thread.visibility === 'restricted' && (
              <div style={{ padding: '0 28px 12px' }}>
                <ThreadRoster thread={thread} />
              </div>
            )}
            <div className="composer">
              {allowTeamMessages && <ComposerMode userId={myUserId} threadId={thread.id} defaultMode={defaultMode} onChange={setComposerMode} />}
              <input
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void send();
                }}
                placeholder={!allowTeamMessages || composerMode === 'agent' ? 'Ask Comrade…' : 'Message the team… @Comrade to ask the AI'}
              />
              <button
                className="btn-ink"
                onClick={() => void send()}
                disabled={sending}
                aria-busy={sending}
              >
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
  const ownHumanMessage = mine && !isAI;
  const [hover, setHover] = useState(false);

  if (cls.kind === 'deleted') {
    return (
      <div
        className="fade-up"
        data-message-side={ownHumanMessage ? 'right' : 'left'}
        style={{ display: 'flex', gap: 14, padding: '10px 28px', justifyContent: ownHumanMessage ? 'flex-end' : 'flex-start' }}
      >
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
      data-message-side={ownHumanMessage ? 'right' : 'left'}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        display: 'flex',
        gap: 14,
        padding: '10px 28px',
        alignItems: 'flex-start',
        justifyContent: ownHumanMessage ? 'flex-end' : 'flex-start',
        flexDirection: ownHumanMessage ? 'row-reverse' : 'row',
      }}
    >
      {isAI ? (
        <AiOrb size={36} />
      ) : (
        <Avatar userId={m.sender_id ?? 'unknown'} name={senderName} size={36} />
      )}
      <div style={{ minWidth: 0, maxWidth: 'min(76%, 700px)' }}>
        <div
          style={{
            background: isAI ? 'rgba(118,85,121,0.09)' : ownHumanMessage ? 'rgba(239,173,154,0.16)' : 'var(--card)',
            border: `1px solid ${isAI ? 'rgba(118,85,121,.22)' : 'var(--border-soft)'}`,
            borderRadius: 12,
            padding: '9px 12px',
            boxShadow: '1px 1px 0 rgba(32,45,53,.06)',
            textAlign: ownHumanMessage ? 'right' : 'left',
          }}
        >
        <div
          style={{
            display: 'flex',
            alignItems: 'baseline',
            justifyContent: ownHumanMessage ? 'flex-end' : 'flex-start',
            gap: 9,
            flexWrap: 'wrap',
          }}
        >
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
                border: '1px solid rgba(118,85,121,0.4)',
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
               remove
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
               remember this
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
                border: '1px solid rgba(199,104,99,0.4)',
                background: 'transparent',
                color: 'var(--terracotta)',
                fontSize: 9,
                letterSpacing: '0.1em',
                borderRadius: 2,
                padding: '2px 7px',
                cursor: 'pointer',
              }}
            >
               REMOVE · DON'T DO THIS AGAIN
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
              <span className="mono">DOC</span>
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
          <div key={d.id}><span className="mono">DOC</span> {d.filename ?? d.kind}</div>
        ))}
        {docs.length === 0 && <span style={{ color: 'var(--faint)' }}>none yet</span>}
      </div>
    </div>
  );
}
