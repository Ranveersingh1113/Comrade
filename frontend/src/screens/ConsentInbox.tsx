import { useCallback, useEffect, useState } from 'react';
import { supabase } from '../lib/supabase';
import { messageTime } from '../lib/format';
import { isActionable, pendingQueueRows } from '../lib/consentModel';
import type { ConsentItem } from '../lib/types';
import { isConfirmedEmpty } from '../lib/listState';
import { useTeam } from '../state/TeamContext';
import { useTeamRealtime } from '../hooks/useRealtime';
import { ConsentCard } from '../components/ConsentCard';

export function ConsentInbox() {
  const { team, myUserId } = useTeam();
  const teamId = team?.id ?? '';
  const [items, setItems] = useState<ConsentItem[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!teamId) return;
    const { data, error: err } = await supabase
      .from('consent_queue')
      .select('*')
      .eq('team_id', teamId)
      .order('created_at', { ascending: false });
    if (err) setError(err.message);
    else setItems((data as ConsentItem[] | null) ?? []);
  }, [teamId]);

  useEffect(() => {
    void load();
  }, [load]);
  useTeamRealtime('consent_queue', teamId, load);

  // Every item needs exactly one key — the requester's (findings §10) — and
  // it has to still be inside its 7-day backstop. An expired item kept
  // status='pending', so it sat here offering an APPROVE button that
  // execute_consent then refuses.
  const isLive = (i: ConsentItem) => isActionable(i);
  const pending = items.filter(isLive);
  const history = items.filter((i) => !isLive(i));

  return (
    <main style={{ flex: 1, overflowY: 'auto' }}>
      <header className="screen-header">
        <div className="display">Consent inbox</div>
        <div className="sub">
          {/* Was "Actions Comrade proposed on your request". Since D4 that is
              not true of every card: a departure request is put here by a
              TEAMMATE, not by Comrade and not on your request. What holds for
              all of them is the second half, which was always the point. */}
          Nothing here runs without your key
        </div>
      </header>
      <div style={{ maxWidth: 720, margin: '0 auto', padding: '24px 32px 44px' }}>
        <div
          className="mono"
          style={{
            display: 'flex',
            gap: 0,
            border: '1px solid var(--border)',
            borderRadius: 3,
            background: 'var(--card)',
            marginBottom: 24,
            fontSize: 9.5,
            letterSpacing: '0.06em',
            color: 'var(--muted)',
          }}
        >
          <span style={{ flex: 1, padding: '9px 12px', borderRight: '1px solid rgba(35,33,48,0.1)' }}>
            <b style={{ color: 'var(--text-soft)' }}>T0</b> READ-ONLY · RUNS INSTANTLY
          </span>
          <span style={{ flex: 1, padding: '9px 12px', borderRight: '1px solid rgba(35,33,48,0.1)' }}>
            <b style={{ color: 'var(--plum)' }}>T1</b> ONE MEMBER · THEY CONSENT
          </span>
          <span style={{ flex: 1, padding: '9px 12px', borderRight: '1px solid rgba(35,33,48,0.1)' }}>
            <b style={{ color: 'var(--terracotta)' }}>T2</b> SHARED · ACT + REVERT
          </span>
        </div>

        {error && (
          <div style={{ fontSize: 12.5, color: 'var(--terracotta)', marginBottom: 14 }}>{error}</div>
        )}

        <div className="micro-label" style={{ marginBottom: 12 }}>
          Pending — {pending.length}
        </div>
        {isConfirmedEmpty(error, items === null, pending.length) && (
          <div
            className="card"
            style={{ padding: '15px 18px', fontSize: 13, color: 'var(--text-soft)', marginBottom: 24 }}
          >
            Queue clear — nothing awaiting your key.
          </div>
        )}
        {pendingQueueRows(items, pending).map((row) =>
          row.kind === 'single' ? (
            <ConsentCard key={row.item.id} item={row.item} onResolved={load} viewerId={myUserId} />
          ) : (
            <div
              key={row.batchId}
              data-testid="consent-batch"
              style={{
                border: '1px solid var(--border)',
                borderRadius: 3,
                padding: '14px 14px 4px',
                marginBottom: 18,
                background: 'rgba(35,33,48,0.02)',
              }}
            >
              <div
                className="mono"
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'baseline',
                  marginBottom: 12,
                  fontSize: 10,
                  letterSpacing: '0.1em',
                  color: 'var(--muted)',
                }}
              >
                <span>ONE PIECE OF WORK · {row.totalCount} RELATED ACTIONS</span>
                <span style={{ color: 'var(--text-soft)' }}>{row.progressLabel.toUpperCase()}</span>
              </div>
              {row.items.map((item) => (
                <ConsentCard key={item.id} item={item} onResolved={load} viewerId={myUserId} />
              ))}
            </div>
          ),
        )}

        {history.length > 0 && (
          <>
            <div className="micro-label" style={{ margin: '26px 0 12px' }}>
              History
            </div>
            <div
              style={{
                background: 'var(--card)',
                border: '1px solid var(--border)',
                borderRadius: 3,
                overflow: 'hidden',
              }}
            >
              {history.map((item) => (
                <div
                  key={item.id}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 12,
                    padding: '12px 16px',
                    borderBottom: '1px solid rgba(35,33,48,0.08)',
                    fontSize: 12,
                  }}
                >
                  <span className="mono" style={{ fontSize: 10, color: 'var(--muted)' }}>
                    Nº {item.id.slice(0, 4).toUpperCase()}
                  </span>
                  <span
                    style={{
                      flex: 1,
                      color: item.status === 'executed' ? 'var(--text)' : 'var(--muted)',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {item.tool_name}
                    {item.source_snippet ? ` — ${item.source_snippet}` : ''}
                  </span>
                  <span
                    className="mono"
                    style={{
                      fontSize: 9,
                      letterSpacing: '0.14em',
                      color: item.status === 'executed' ? 'var(--sage)' : 'var(--faint)',
                      border: `1px solid ${
                        item.status === 'executed'
                          ? 'rgba(95,138,110,0.4)'
                          : 'rgba(169,165,176,0.4)'
                      }`,
                      borderRadius: 2,
                      padding: '2px 7px',
                      flex: 'none',
                    }}
                  >
                    {item.status.toUpperCase()} ·{' '}
                    {messageTime(item.resolved_at ?? item.created_at).toUpperCase()}
                  </span>
                </div>
              ))}
            </div>
          </>
        )}

        <div style={{ fontSize: 11, color: 'var(--faint)', lineHeight: 1.6, padding: '12px 4px 0' }}>
          Every item shows the literal tool and arguments — what you approve is exactly what runs,
          hash-verified. Items expire after 7 days untouched.
        </div>
      </div>
    </main>
  );
}
