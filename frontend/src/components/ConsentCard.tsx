import { useState } from 'react';
import { AgentApiError, approveConsent, rejectConsent } from '../lib/agentApi';
import { consentPhase } from '../lib/consentModel';
import { countdown, messageTime } from '../lib/format';
import type { ConsentItem } from '../lib/types';

type ApprovalSummary = {
  eyebrow: string;
  title: string;
  description: string;
  fields: Array<{ label: string; value: string }>;
};

function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function approvalSummary(item: ConsentItem): ApprovalSummary {
  const args = item.tool_args;
  const title = text(args.title);
  const description = text(args.description);
  const deadline = text(args.deadline);

  if (item.tool_name === 'task_create') {
    return {
      eyebrow: 'New task',
      title: title ? `Create “${title}”` : 'Create a new task',
      description: description ?? 'Comrade will add this task to the team plan.',
      fields: deadline ? [{ label: 'Due', value: deadline }] : [],
    };
  }
  if (item.tool_name === 'task_update') {
    const changes = [
      title && `rename it to “${title}”`,
      Object.hasOwn(args, 'description') && `update its details${description ? ` to “${description}”` : ''}`,
      Object.hasOwn(args, 'deadline') && (deadline ? `set its due date to ${deadline}` : 'clear its due date'),
      text(args.assignee_id) && 'assign it to a teammate',
    ].filter(Boolean);
    return {
      eyebrow: 'Task update',
      title: 'Update an existing task',
      description: changes.length ? `Comrade will ${changes.join(', ')}.` : 'Comrade will update an existing task.',
      fields: [],
    };
  }
  if (item.tool_name === 'repo_open_pr') {
    const patch = text(args.patch);
    const fileCount = patch
      ? new Set([...patch.matchAll(/^\+\+\+ b\/(.+)$/gm)].map((match) => match[1])).size
      : 0;
    return {
      eyebrow: 'Pull request',
      title: title ? `Open “${title}” for review` : 'Open a pull request for review',
      description: text(args.body) ?? 'Comrade will open the prepared changes for review; nothing will be merged.',
      fields: [
        ...(text(args.repo_full_name) ? [{ label: 'Repository', value: text(args.repo_full_name)! }] : []),
        ...(fileCount ? [{ label: 'Prepared changes', value: `${fileCount} file${fileCount === 1 ? '' : 's'} changed` }] : []),
      ],
    };
  }
  return {
    eyebrow: 'Requested action',
    title: 'Review a requested action',
    description: 'Comrade is asking for approval before completing this action.',
    fields: [],
  };
}

/** A readable approval summary. Technical action details remain internal to the consent contract. */
export function ConsentCard({
  item,
  onResolved,
  viewerId = null,
}: {
  item: ConsentItem;
  onResolved: () => void;
  viewerId?: string | null;
}) {
  const [rejectReason, setRejectReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const phase = consentPhase(item, viewerId, stale);
  const pending = phase === 'pending';
  const canResolve = pending && item.requesting_member_id === viewerId;
  const canGrantForThread = item.tool_name === 'task_create' || item.tool_name === 'task_update';
  const executed = phase === 'executed';
  const dead = phase === 'rejected' || phase === 'cancelled' || phase === 'expired';
  const summary = approvalSummary(item);
  const badge = pending
    ? { text: canResolve ? 'AWAITING YOUR APPROVAL' : 'AWAITING REQUESTER APPROVAL', color: 'var(--peach-pale)' }
    : executed ? { text: 'COMPLETED', color: 'var(--peach)' }
      : item.status === 'approved' || item.status === 'edited' ? { text: 'IN PROGRESS', color: 'var(--lavender)' }
       : { text: 'CLOSED', color: 'var(--muted)' };

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onResolved();
    } catch (e) {
      if (e instanceof AgentApiError && e.status === 409) {
        setStale(true);
        setError('This request went stale — it expired or changed. Ask Comrade for a fresh request.');
        onResolved();
      } else if (e instanceof AgentApiError && e.status === 404) {
        setError('This request is no longer pending — it was resolved elsewhere.');
        onResolved();
      } else setError(e instanceof Error ? e.message : 'Unable to complete this action.');
    } finally {
      setBusy(false);
    }
  };

  return <div data-testid="consent-card" data-consent-id={item.id} className="consent-card" style={{ position: 'relative', marginBottom: 18 }}>
    <div style={{ background: 'var(--card)', border: '1px solid var(--border-strong)', borderRadius: 3, boxShadow: 'var(--shadow-card-strong)', overflow: 'hidden' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '11px 16px', background: 'var(--ink)', color: 'var(--paper)' }}>
        <span className="mono" style={{ fontSize: 10, letterSpacing: '0.16em' }}>{summary.eyebrow.toUpperCase()}</span>
        <span className="mono" style={{ fontSize: 9, letterSpacing: '0.1em', color: item.reversible ? 'var(--lavender)' : 'var(--peach-pale)' }}>{item.reversible ? 'REVERSIBLE' : 'IRREVERSIBLE'}</span>
        <span className="mono" style={{ marginLeft: 'auto', fontSize: 10, letterSpacing: '0.1em', color: badge.color }}>{badge.text}</span>
      </div>
      <div style={{ padding: '15px 16px 16px' }}>
        <div style={{ fontSize: 16, fontWeight: 700, color: 'var(--text)' }}>{summary.title}</div>
        <p style={{ margin: '5px 0 0', fontSize: 13, lineHeight: 1.5, color: 'var(--text-body)' }}>{summary.description}</p>
        {summary.fields.map((field) => <div key={field.label} style={{ marginTop: 9, fontSize: 12, color: 'var(--text-body)' }}><b>{field.label}:</b> {field.value}</div>)}
        {item.source_snippet && <div style={{ borderLeft: '2px solid rgba(35,33,48,0.2)', padding: '2px 0 2px 12px', fontSize: 11.5, fontStyle: 'italic', color: 'var(--muted)', marginTop: 13 }}>“{item.source_snippet}” — {messageTime(item.created_at)}</div>}
        {item.expires_at && <div className="mono" style={{ marginTop: 12, fontSize: 10, color: 'var(--faint)' }}>EXPIRES IN {countdown(item.expires_at)}</div>}
        {error && <div style={{ marginTop: 12, fontSize: 12, lineHeight: 1.5, color: stale ? 'var(--plum)' : 'var(--terracotta)' }}>{stale && <span className="mono" style={{ display: 'block', fontSize: 9.5, letterSpacing: '0.14em', marginBottom: 4 }}>STALE — NOTHING RAN</span>}{error}</div>}
        {canResolve && !stale && <>
          <input value={rejectReason} onChange={(e) => setRejectReason(e.target.value)} placeholder="Reason for declining (optional)" className="mono" style={{ display: 'block', width: '100%', boxSizing: 'border-box', marginTop: 15, border: '1px solid var(--border-strong)', borderRadius: 3, padding: '7px 10px', fontSize: 11, background: '#fff' }} />
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 9, marginTop: 15 }}>
            <button className="btn-primary" aria-label="Approve once" disabled={busy} onClick={() => void act(() => approveConsent(item.id, item.team_id))}>Approve</button>
            {canGrantForThread && <button className="btn-secondary" aria-label="Approve for this thread" disabled={busy} onClick={() => void act(() => approveConsent(item.id, item.team_id, true))}>Approve for thread</button>}
            <button className="btn-ghost" aria-label="Reject" disabled={busy} onClick={() => void act(() => rejectConsent(item.id, item.team_id, rejectReason.trim() || undefined))}>Decline</button>
          </div>
        </>}
        {executed && <div style={{ marginTop: 15, fontSize: 12.5, color: 'var(--terracotta)' }}>✓ Completed</div>}
        {dead && <div style={{ marginTop: 15, fontSize: 12.5, color: 'var(--muted)' }}>{phase === 'expired' ? 'Expired — nothing ran.' : item.status === 'rejected' ? item.resolution_reason ? `Declined — ${item.resolution_reason}` : 'Declined — nothing ran.' : 'Cancelled.'}</div>}
      </div>
    </div>
    {executed && <div className="stamp">DONE</div>}
    {dead && <div className="stamp void">VOID</div>}
  </div>;
}