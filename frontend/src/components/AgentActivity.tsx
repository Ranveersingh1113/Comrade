import { useState } from 'react';
import type { AgentStep } from '../lib/agentApi';
import { activityLabel } from '../lib/toolActivity';
import { DiffView } from './DiffView';

const SENSITIVE = /token|secret|password|authorization|cookie|key/i;

function redact(value: unknown, key = ''): unknown {
  if (SENSITIVE.test(key)) return '[redacted]';
  if (Array.isArray(value)) return value.map((item) => redact(item));
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([name, item]) => [name, redact(item, name)]));
  }
  return value;
}

function detail(value: unknown): string {
  return JSON.stringify(redact(value), null, 2);
}

function patch(step: AgentStep): string | null {
  const args = step.args as Record<string, unknown> | undefined;
  const response = step.response as Record<string, unknown> | undefined;
  const value = args?.patch ?? response?.patch;
  return typeof value === 'string' && value.includes('diff --git') ? value : null;
}

function ActivityItem({ step }: { step: AgentStep }) {
  const [open, setOpen] = useState(false);
  const label = activityLabel(step.tool ?? 'Comrade');
  const payload = step.type === 'tool_call' ? step.args : step.response;
  const diff = patch(step);
  return (
    <section className="agent-activity-item" style={{ margin: '8px 28px', border: '1px solid var(--border-soft)', borderRadius: 8, overflow: 'hidden' }}>
      <button
        type="button"
        aria-expanded={open}
        aria-label={`Details for ${label}`}
        onClick={() => setOpen((value) => !value)}
        style={{ width: '100%', display: 'flex', gap: 9, border: 0, background: 'var(--paper)', padding: '9px 11px', color: 'var(--text-body)', cursor: 'pointer', textAlign: 'left' }}
      >
         <span aria-hidden className="mono">{open ? '−' : '+'}</span>
        <span>{label}</span>
        <span className="mono" style={{ marginLeft: 'auto', color: 'var(--faint)', fontSize: 10 }}>{step.type === 'tool_call' ? 'running' : 'result'}</span>
      </button>
      {open && <div style={{ borderTop: '1px solid var(--border-soft)', padding: 11 }}>
        {diff ? <DiffView patch={diff} /> : <pre style={{ margin: 0, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 11, color: 'var(--text-soft)' }}>{detail(payload ?? {})}</pre>}
      </div>}
    </section>
  );
}

/** Durable stream frames rendered as human-readable, inspectable work. */
export function AgentActivity({ steps }: { steps: AgentStep[] }) {
  if (!steps.length) return null;
  return <div aria-label="Comrade activity">{steps.map((step, index) => <ActivityItem key={`${step.seq ?? index}:${step.type}`} step={step} />)}</div>;
}
