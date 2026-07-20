import { useState } from 'react';
import {
  AgentApiError,
  approveConsent,
  editAndApproveConsent,
  rejectConsent,
  secondKeyConsent,
} from '../lib/agentApi';
import { consentPhase } from '../lib/consentModel';
import { countdown, messageTime, shortHash } from '../lib/format';
import type { ConsentItem } from '../lib/types';

/**
 * A warrant card. Hard design rule: always the LITERAL tool name, LITERAL
 * args and the source snippet — what you approve is exactly what runs.
 */
export function ConsentCard({
  item,
  onResolved,
  viewerId = null,
}: {
  item: ConsentItem;
  onResolved: () => void;
  viewerId?: string | null;
}) {
  const [expanded, setExpanded] = useState(false);
  const [editing, setEditing] = useState(false);
  const [argsDraft, setArgsDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);

  const phase = consentPhase(item, viewerId, stale);
  const pending = phase === 'pending';
  const executed = phase === 'executed';
  const dead = phase === 'rejected' || phase === 'cancelled';

  const badge =
    phase === 'pending'
      ? { text: 'AWAITING YOUR KEY', color: 'var(--peach-pale)' }
      : phase === 'can_countersign'
        ? { text: 'T3 · NEEDS YOUR COUNTERSIGN', color: 'var(--peach-pale)' }
        : phase === 'awaiting_second_key'
          ? { text: 'T3 · AWAITING SECOND KEY', color: 'var(--lavender)' }
          : phase === 'countersigned_pending'
            ? { text: 'T3 · COUNTERSIGNED — AWAITING REQUESTER', color: 'var(--lavender)' }
            : executed
              ? { text: 'EXECUTED', color: 'var(--peach)' }
              : item.status === 'approved' || item.status === 'edited'
                ? { text: 'EXECUTING…', color: 'var(--lavender)' }
                : { text: 'VOID', color: '#908B9E' };

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onResolved();
    } catch (e) {
      if (e instanceof AgentApiError && e.status === 409) {
        // Expired, or the action hash no longer matches what was proposed.
        setStale(true);
        setError(
          'This request went stale — it expired or its contents changed since it was proposed. Ask Comrade again to get a fresh one.',
        );
        onResolved();
      } else if (e instanceof AgentApiError && e.status === 404) {
        setError('This item is no longer pending — someone or something already resolved it.');
        onResolved();
      } else {
        setError(e instanceof Error ? e.message : 'Action failed');
      }
    } finally {
      setBusy(false);
    }
  };

  const startEdit = () => {
    setArgsDraft(JSON.stringify(item.tool_args, null, 2));
    setEditing(true);
  };

  const submitEdit = () => {
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(argsDraft) as Record<string, unknown>;
    } catch {
      setError('Args must be valid JSON.');
      return;
    }
    void act(() => editAndApproveConsent(item.id, item.team_id, parsed));
  };

  return (
    <div
      data-testid="consent-card"
      data-consent-id={item.id}
      style={{ position: 'relative', marginBottom: 18 }}
    >
      <div
        style={{
          background: 'var(--card)',
          border: '1px solid var(--border-strong)',
          borderRadius: 3,
          boxShadow: 'var(--shadow-card-strong)',
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            padding: '11px 16px',
            background: 'var(--ink)',
            color: 'var(--paper)',
          }}
        >
          <span className="mono" style={{ fontSize: 10, letterSpacing: '0.2em' }}>
            Nº {item.id.slice(0, 4).toUpperCase()} · {item.tool_name}
          </span>
          <span
            className="mono"
            style={{
              fontSize: 9,
              letterSpacing: '0.1em',
              color: item.reversible ? 'var(--lavender)' : 'var(--peach-pale)',
              border: `1px solid ${item.reversible ? 'rgba(196,180,218,0.5)' : 'rgba(240,185,164,0.5)'}`,
              borderRadius: 2,
              padding: '2px 6px',
            }}
          >
            {item.reversible ? 'REVERSIBLE' : 'IRREVERSIBLE'}
          </span>
          <span
            className="mono"
            style={{ marginLeft: 'auto', fontSize: 10, letterSpacing: '0.1em', color: badge.color }}
          >
            {badge.text}
          </span>
        </div>

        <div style={{ padding: '15px 16px 16px' }}>
          {item.source_snippet && (
            <div
              style={{
                borderLeft: '2px solid rgba(35,33,48,0.2)',
                padding: '2px 0 2px 12px',
                fontSize: 11.5,
                fontStyle: 'italic',
                color: 'var(--muted)',
                marginBottom: 12,
              }}
            >
              “{item.source_snippet}” — {messageTime(item.created_at)}
            </div>
          )}

          {editing ? (
            <>
              <textarea
                value={argsDraft}
                onChange={(e) => setArgsDraft(e.target.value)}
                rows={6}
                className="mono"
                style={{
                  width: '100%',
                  border: '1.5px solid var(--terracotta)',
                  borderRadius: 3,
                  padding: '11px 13px',
                  fontSize: 12,
                  lineHeight: 1.55,
                  color: 'var(--text)',
                  outline: 'none',
                  resize: 'vertical',
                  background: '#fff',
                }}
              />
              <div className="mono" style={{ fontSize: 10, color: 'var(--plum)', marginTop: 6 }}>
                EDITING RE-STAMPS THE ACTION HASH — WHAT EXECUTES IS EXACTLY WHAT YOU APPROVE
              </div>
            </>
          ) : (
            <div
              className="mono"
              style={{
                background: 'var(--ink)',
                borderRadius: 3,
                padding: '13px 15px',
                fontSize: 11,
                lineHeight: 1.75,
                color: '#C6C2CE',
                overflowX: 'auto',
              }}
            >
              <div>
                <span style={{ color: 'var(--peach)' }}>tool</span>{'      '}
                {item.tool_name}
              </div>
              {Object.entries(item.tool_args).map(([k, v]) => (
                <div key={k}>
                  <span style={{ color: 'var(--peach)' }}>{k}</span>{' '}
                  {typeof v === 'string' ? v : JSON.stringify(v)}
                </div>
              ))}
              <div>
                <span style={{ color: 'var(--peach)' }}>hash</span>{'      '}sha256:
                {shortHash(item.action_hash)}{' '}
                <span style={{ color: 'var(--ink-faint)' }}>(re-verified at execute)</span>
              </div>
              {item.expires_at && (
                <div>
                  <span style={{ color: 'var(--peach)' }}>expires</span>{'   '}
                  {countdown(item.expires_at)}
                </div>
              )}
            </div>
          )}

          <button
            onClick={() => setExpanded((x) => !x)}
            className="mono"
            style={{
              border: 'none',
              background: 'transparent',
              padding: '11px 0 0',
              fontSize: 10.5,
              letterSpacing: '0.1em',
              color: 'var(--terracotta)',
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: 6,
            }}
          >
            <span style={{ fontSize: 9 }}>{expanded ? '▾' : '▸'}</span> RAW ARGS — EXACT JSON
          </button>
          {expanded && (
            <pre
              className="mono"
              style={{
                margin: '9px 0 0',
                background: 'var(--ink)',
                borderRadius: 3,
                padding: '13px 15px',
                fontSize: 11,
                lineHeight: 1.6,
                color: '#C6C2CE',
                overflowX: 'auto',
              }}
            >
              {JSON.stringify(item.tool_args, null, 2)}
            </pre>
          )}

          {error && (
            <div
              style={{
                marginTop: 12,
                fontSize: 12,
                lineHeight: 1.5,
                color: stale ? 'var(--plum)' : 'var(--terracotta)',
                border: stale ? '1px dashed rgba(110,95,135,0.5)' : 'none',
                borderRadius: stale ? 2 : 0,
                padding: stale ? '9px 11px' : 0,
              }}
            >
              {stale && (
                <span className="mono" style={{ display: 'block', fontSize: 9.5, letterSpacing: '0.14em', marginBottom: 4 }}>
                  STALE — NOTHING RAN
                </span>
              )}
              {error}
            </div>
          )}

          {pending && !stale && (
            <div style={{ display: 'flex', gap: 9, marginTop: 15, alignItems: 'center' }}>
              {editing ? (
                <>
                  <button className="btn-primary" disabled={busy} onClick={submitEdit}>
                    APPROVE EDITED
                  </button>
                  <button
                    className="btn-secondary"
                    disabled={busy}
                    onClick={() => setEditing(false)}
                  >
                    Cancel edit
                  </button>
                </>
              ) : (
                <>
                  <button
                    className="btn-primary"
                    disabled={busy}
                    onClick={() => void act(() => approveConsent(item.id, item.team_id))}
                  >
                    APPROVE
                  </button>
                  <button className="btn-secondary" disabled={busy} onClick={startEdit}>
                    Edit
                  </button>
                  <button
                    className="btn-ghost"
                    disabled={busy}
                    onClick={() => void act(() => rejectConsent(item.id, item.team_id))}
                  >
                    Reject
                  </button>
                </>
              )}
              {item.expires_at && (
                <span
                  className="mono"
                  style={{ marginLeft: 'auto', fontSize: 9.5, color: 'var(--faint)' }}
                >
                  EXPIRES IN {countdown(item.expires_at)}
                </span>
              )}
            </div>
          )}
          {phase === 'can_countersign' && !stale && (
            <div style={{ display: 'flex', gap: 9, marginTop: 15, alignItems: 'center' }}>
              <button
                className="btn-primary"
                disabled={busy}
                onClick={() => void act(() => secondKeyConsent(item.id, item.team_id))}
              >
                COUNTERSIGN — SECOND KEY
              </button>
              <span style={{ fontSize: 11, color: 'var(--faint)', fontStyle: 'italic' }}>
                T3 actions need two members. You are not approving your own request.
              </span>
            </div>
          )}
          {phase === 'awaiting_second_key' && (
            <div style={{ marginTop: 15, fontSize: 12.5, color: 'var(--muted)' }}>
              Approved by the requester — waiting on any other member's countersign.
            </div>
          )}
          {phase === 'countersigned_pending' && (
            <div style={{ marginTop: 15, fontSize: 12.5, color: 'var(--muted)' }}>
              Countersigned — waiting on the requester's approval.
            </div>
          )}
          {executed && (
            <div style={{ marginTop: 15, fontSize: 12.5, color: 'var(--terracotta)' }}>
              ✓ Executed
            </div>
          )}
          {dead && (
            <div style={{ marginTop: 15, fontSize: 12.5, color: 'var(--muted)' }}>
              {item.status === 'rejected' ? 'Rejected — nothing ran.' : 'Cancelled.'}
            </div>
          )}
        </div>
      </div>
      {executed && <div className="stamp">DONE</div>}
      {dead && <div className="stamp void">VOID</div>}
    </div>
  );
}
