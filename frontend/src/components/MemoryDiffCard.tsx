import { useState } from 'react';
import { Link } from 'react-router-dom';
import { supabase } from '../lib/supabase';
import type { MemoryCompilation, MemoryVersion } from '../lib/types';
import { useTeam } from '../state/TeamContext';

interface DiffRow {
  version: MemoryVersion;
  previousFact: string | null;
  revertQueued: boolean;
}

/**
 * Expandable "memory updated" card attached to a compilation's diff message.
 * One-tap revert = insert into memory_reverts (members insert their own;
 * the pipeline honours it on the next compile — it never silently edits).
 */
export function MemoryDiffCard({ compilation }: { compilation: MemoryCompilation }) {
  const { myUserId } = useTeam();
  const [expanded, setExpanded] = useState(false);
  const [rows, setRows] = useState<DiffRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadRows = async () => {
    const { data: versions, error: vErr } = await supabase
      .from('memory_versions')
      .select('*')
      .eq('compilation_id', compilation.id)
      .order('created_at');
    if (vErr) {
      setError(vErr.message);
      return;
    }
    const vs = (versions as MemoryVersion[] | null) ?? [];
    const entryIds = vs.map((v) => v.entry_id);
    const [{ data: reverts }, { data: priors }] = await Promise.all([
      supabase.from('memory_reverts').select('entry_id').in('entry_id', entryIds),
      supabase
        .from('memory_versions')
        .select('*')
        .in('entry_id', entryIds)
        .eq('is_active', false)
        .order('created_at', { ascending: false }),
    ]);
    const revertedEntries = new Set(
      ((reverts as Array<{ entry_id: string }> | null) ?? []).map((r) => r.entry_id),
    );
    const priorByEntry = new Map<string, MemoryVersion>();
    for (const p of (priors as MemoryVersion[] | null) ?? []) {
      // first hit per entry = most recent inactive version
      if (!priorByEntry.has(p.entry_id) && p.id !== vs.find((v) => v.entry_id === p.entry_id)?.id) {
        priorByEntry.set(p.entry_id, p);
      }
    }
    setRows(
      vs.map((v) => ({
        version: v,
        previousFact:
          v.change_type === 'revised' ? (priorByEntry.get(v.entry_id)?.fact ?? null) : null,
        revertQueued: revertedEntries.has(v.entry_id),
      })),
    );
  };

  const toggle = () => {
    const next = !expanded;
    setExpanded(next);
    if (next && rows === null) void loadRows();
  };

  const queueRevert = async (row: DiffRow) => {
    const { error: insErr } = await supabase.from('memory_reverts').insert({
      entry_id: row.version.entry_id,
      team_id: compilation.team_id,
      member_id: myUserId,
      reverted_version_id: row.version.id,
    });
    if (insErr) {
      setError(insErr.message);
      return;
    }
    setRows(
      (prev) =>
        prev?.map((r) =>
          r.version.entry_id === row.version.entry_id ? { ...r, revertQueued: true } : r,
        ) ?? null,
    );
  };

  const tagOf = (v: MemoryVersion) =>
    v.change_type === 'added'
      ? { tag: 'ADD', color: 'var(--sage)' }
      : v.change_type === 'revised'
        ? { tag: 'REV', color: 'var(--plum)' }
        : { tag: 'RVT', color: 'var(--muted)' };

  return (
    <div
      style={{
        marginTop: 9,
        background: 'var(--card)',
        border: '1px solid rgba(35,33,48,0.2)',
        borderRadius: 3,
        maxWidth: 540,
        boxShadow: '3px 3px 0 rgba(35,33,48,0.1)',
        overflow: 'hidden',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          padding: '9px 15px',
          background: 'var(--ink)',
          color: 'var(--paper)',
        }}
      >
        <span className="mono" style={{ fontSize: 9.5, letterSpacing: '0.2em' }}>
          MEMORY UPDATED · Nº {compilation.id.slice(0, 4).toUpperCase()}
        </span>
        <button
          onClick={toggle}
          className="mono"
          style={{
            marginLeft: 'auto',
            border: 'none',
            background: 'transparent',
            color: 'var(--peach-pale)',
            fontSize: 10,
            letterSpacing: '0.1em',
            cursor: 'pointer',
          }}
        >
          {expanded ? 'HIDE CHANGES ▾' : 'VIEW CHANGES ▸'}
        </button>
      </div>
      <div style={{ padding: '11px 15px', fontSize: 12.5, color: 'var(--text-soft)' }}>
        {compilation.entries_added} added · {compilation.entries_revised} revised ·{' '}
        {compilation.entries_removed} removed — every change is revertible in the{' '}
        <Link to="../wiki">team wiki</Link>.
      </div>
      {expanded && (
        <div
          style={{
            borderTop: '1px dashed rgba(35,33,48,0.15)',
            padding: '11px 15px',
            display: 'flex',
            flexDirection: 'column',
            gap: 9,
          }}
        >
          {error && <div style={{ fontSize: 12, color: 'var(--terracotta)' }}>{error}</div>}
          {rows === null && !error && (
            <div style={{ fontSize: 12, color: 'var(--faint)' }}>Loading changes…</div>
          )}
          {rows?.length === 0 && (
            <div style={{ fontSize: 12, color: 'var(--faint)' }}>No versioned changes found.</div>
          )}
          {rows?.map((row) => {
            const t = tagOf(row.version);
            return (
              <div
                key={row.version.id}
                style={{
                  display: 'flex',
                  gap: 10,
                  alignItems: 'flex-start',
                  fontSize: 12.5,
                  lineHeight: 1.5,
                }}
              >
                <span
                  className="mono"
                  style={{
                    flex: 'none',
                    fontSize: 9,
                    fontWeight: 700,
                    letterSpacing: '0.12em',
                    padding: '3px 6px',
                    borderRadius: 2,
                    color: t.color,
                    border: `1px solid ${t.color}`,
                    marginTop: 1,
                  }}
                >
                  {t.tag}
                </span>
                <span style={{ flex: 1, color: 'var(--text-body)' }}>
                  {row.version.fact}
                  {row.previousFact && (
                    <span
                      style={{
                        display: 'block',
                        fontSize: 11,
                        color: 'var(--faint)',
                        textDecoration: 'line-through',
                        marginTop: 2,
                      }}
                    >
                      {row.previousFact}
                    </span>
                  )}
                </span>
                {row.revertQueued ? (
                  <span className="mono" style={{ flex: 'none', fontSize: 9.5, color: 'var(--plum)' }}>
                    REVERT QUEUED
                  </span>
                ) : (
                  <button
                    onClick={() => void queueRevert(row)}
                    className="mono"
                    style={{
                      flex: 'none',
                      border: '1px solid rgba(35,33,48,0.25)',
                      background: '#fff',
                      color: 'var(--text-soft)',
                      fontSize: 10.5,
                      borderRadius: 2,
                      padding: '3px 8px',
                      cursor: 'pointer',
                    }}
                  >
                     queue revert
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
