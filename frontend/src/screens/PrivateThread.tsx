import { useEffect, useRef, useState } from 'react';
import { streamTurn, agentErrorText } from '../lib/agentApi';
import { messageTime } from '../lib/format';
import { useTeam } from '../state/TeamContext';
import { useMessages } from '../hooks/useMessages';
import { Avatar, AiOrb } from '../components/Avatar';

export function PrivateThread() {
  const { team, myUserId, profileOf } = useTeam();
  const { messages, refresh, error } = useMessages('private');
  const [draft, setDraft] = useState('');
  const [waiting, setWaiting] = useState(false);
  const [pending, setPending] = useState('');
  const [step, setStep] = useState('');
  const [sendError, setSendError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const me = profileOf(myUserId);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length, waiting]);

  const send = async () => {
    const text = draft.trim();
    if (!text || !team) return;
    setDraft('');
    setSendError(null);
    setWaiting(true);
    setPending('');
    setStep('');
    try {
      // Server persists both sides of the exchange; refresh/Realtime shows them.
      // The stream is only for watching it happen.
      await streamTurn(team.id, text, 'private', (f) => {
        if (f.type === 'text') setPending((p) => p + (f.text ?? ''));
        else if (f.type === 'tool_call') setStep(`checking ${f.tool}…`);
        else if (f.type === 'error') setSendError(f.detail ?? 'Turn failed');
      });
    } catch (e) {
      setSendError(agentErrorText(e));
      setDraft(text);
    } finally {
      setWaiting(false);
      setPending('');
      setStep('');
      await refresh();
    }
  };

  return (
    <main style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
      <header
        style={{
          flex: 'none',
          display: 'flex',
          alignItems: 'center',
          gap: 14,
          padding: '20px 28px 14px',
          borderBottom: '1px solid var(--border-soft)',
        }}
      >
        <AiOrb size={38} breathing />
        <div>
          <div className="display" style={{ fontSize: 30 }}>
            Private thread
          </div>
          <div style={{ fontSize: 11, letterSpacing: '0.06em', color: 'var(--muted)', marginTop: 5 }}>
            Only you can see this — not teammates, not leads
          </div>
        </div>
      </header>

      <div ref={scrollRef} style={{ flex: 1, overflowY: 'auto', padding: '26px 0' }}>
        <div
          style={{
            maxWidth: 660,
            margin: '0 auto',
            padding: '0 28px',
            display: 'flex',
            flexDirection: 'column',
            gap: 18,
          }}
        >
          {error && <div style={{ fontSize: 12, color: 'var(--terracotta)' }}>{error}</div>}
          {messages.length === 0 && !error && (
            <div style={{ fontSize: 13, color: 'var(--faint)', lineHeight: 1.6 }}>
              Nothing here yet. Ask Comrade anything — deadlines, who owns what, a draft for the
              group. Nudges also arrive here, privately.
            </div>
          )}
          {messages.map((m) => {
            const isAI = m.sender_kind === 'ai';
            return (
              <div
                key={m.id}
                className="fade-up"
                // A stable hook for the e2e turn test. It previously asserted
                // the reply contained the word "task", which depends on how
                // the model happens to phrase itself and flaked three times.
                data-sender={isAI ? 'ai' : 'user'}
                style={{
                  display: 'flex',
                  gap: 13,
                  flexDirection: isAI ? 'row' : 'row-reverse',
                }}
              >
                {isAI ? (
                  <AiOrb size={32} />
                ) : (
                  <Avatar userId={myUserId} name={me?.display_name ?? 'You'} size={32} />
                )}
                <div
                  style={
                    isAI
                      ? {
                          background: 'var(--card)',
                          border: '1px solid var(--border)',
                          borderRadius: 3,
                          boxShadow: '3px 3px 0 rgba(35,33,48,0.08)',
                          padding: '13px 16px',
                          fontSize: 13.5,
                          lineHeight: 1.55,
                          color: 'var(--text-body)',
                          maxWidth: 470,
                          whiteSpace: 'pre-wrap',
                          overflowWrap: 'anywhere',
                        }
                      : {
                          background: 'var(--ink)',
                          color: 'var(--paper)',
                          borderRadius: 3,
                          padding: '13px 16px',
                          fontSize: 13.5,
                          lineHeight: 1.55,
                          maxWidth: 470,
                          boxShadow: '3px 3px 0 rgba(35,33,48,0.15)',
                          whiteSpace: 'pre-wrap',
                          overflowWrap: 'anywhere',
                        }
                  }
                >
                  {m.body}
                  <div
                    className="mono"
                    style={{
                      fontSize: 10,
                      color: isAI ? 'var(--faint)' : 'var(--ink-muted)',
                      marginTop: 9,
                      textAlign: isAI ? 'left' : 'right',
                    }}
                  >
                    {messageTime(m.created_at).toUpperCase()}
                  </div>
                </div>
              </div>
            );
          })}
          {waiting && (
            <div style={{ display: 'flex', gap: 13 }}>
              <AiOrb size={32} breathing />
              {pending ? (
                <div
                  style={{
                    background: 'var(--card)',
                    border: '1px solid var(--border)',
                    borderRadius: 3,
                    boxShadow: '3px 3px 0 rgba(35,33,48,0.08)',
                    padding: '13px 16px',
                    fontSize: 13.5,
                    lineHeight: 1.55,
                    color: 'var(--text-body)',
                    maxWidth: 470,
                    whiteSpace: 'pre-wrap',
                    overflowWrap: 'anywhere',
                  }}
                >
                  {pending}
                </div>
              ) : (
                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                  <span style={{ display: 'flex', gap: 4 }}>
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
                  </span>
                  {step && (
                    <span className="mono" style={{ fontSize: 10, color: 'var(--faint)' }}>
                      {step}
                    </span>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      <div
        style={{
          flex: 'none',
          padding: '12px 28px 18px',
          maxWidth: 716,
          margin: '0 auto',
          width: '100%',
        }}
      >
        {sendError && (
          <div style={{ fontSize: 12, color: 'var(--terracotta)', marginBottom: 8 }}>
            {sendError}
          </div>
        )}
        <div className="composer">
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void send();
            }}
            placeholder="Ask Comrade anything — this stays private"
          />
          <button className="btn-ink" onClick={() => void send()} disabled={waiting}>
            SEND
          </button>
        </div>
      </div>
    </main>
  );
}
