import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { supabase } from '../lib/supabase';
import { messageTime } from '../lib/format';
import type {
  MemoryCitation,
  MemoryCompilation,
  MemoryEntry,
  MemoryPage,
  MemoryVersion,
} from '../lib/types';
import { useTeam } from '../state/TeamContext';
import {
  factProvenance,
  projectWiki,
  type WikiFact,
  type WikiPage as WikiPageView,
} from '../lib/wikiModel';

// Projection logic lives in lib/wikiModel (pure, unit-tested).
type Fact = WikiFact;
type PageView = WikiPageView;

export function Wiki() {
  const { team, myUserId } = useTeam();
  const teamId = team?.id ?? '';
  const [pages, setPages] = useState<PageView[] | null>(null);
  const [lastCompile, setLastCompile] = useState<MemoryCompilation | null>(null);
  const [expandedFact, setExpandedFact] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!teamId) return;
    const [pagesRes, entriesRes, versionsRes, revertsRes, compileRes] = await Promise.all([
      supabase.from('memory_pages').select('*').eq('team_id', teamId).order('title'),
      supabase.from('memory_entries').select('*').eq('team_id', teamId).eq('archived', false),
      supabase.from('memory_versions').select('*').eq('team_id', teamId).order('created_at'),
      supabase.from('memory_reverts').select('*').eq('team_id', teamId),
      supabase
        .from('memory_compilations')
        .select('*')
        .eq('team_id', teamId)
        .eq('status', 'done')
        .order('started_at', { ascending: false })
        .limit(1),
    ]);
    const err =
      pagesRes.error ?? entriesRes.error ?? versionsRes.error ?? revertsRes.error ?? null;
    if (err) {
      setError(err.message);
      return;
    }
    const pageRows = (pagesRes.data as MemoryPage[] | null) ?? [];
    const entries = (entriesRes.data as MemoryEntry[] | null) ?? [];
    const versions = (versionsRes.data as MemoryVersion[] | null) ?? [];
    const reverts = (revertsRes.data as Array<{ entry_id: string }> | null) ?? [];
    setLastCompile(((compileRes.data as MemoryCompilation[] | null) ?? [])[0] ?? null);

    const activeIds = versions.filter((v) => v.is_active).map((v) => v.id);
    let citations: MemoryCitation[] = [];
    if (activeIds.length > 0) {
      const { data: cits } = await supabase
        .from('memory_citations')
        .select('*')
        .in('version_id', activeIds);
      citations = (cits as MemoryCitation[] | null) ?? [];
    }
    setPages(projectWiki(pageRows, entries, versions, citations, reverts));
    setError(null);
  }, [teamId]);

  useEffect(() => {
    void load();
  }, [load]);

  const queueRevert = async (fact: Fact) => {
    const { error: err } = await supabase.from('memory_reverts').insert({
      entry_id: fact.entryId,
      team_id: teamId,
      member_id: myUserId,
      reverted_version_id: fact.active.id,
    });
    if (err) setError(err.message);
    else await load();
  };

  const citeLabel = (c: MemoryCitation) =>
    c.source_kind === 'message' ? 'MSG' : c.source_kind === 'document' ? 'DOC' : 'GITHUB';

  return (
    <main style={{ flex: 1, overflowY: 'auto' }}>
      <header className="screen-header" style={{ display: 'flex', alignItems: 'flex-end' }}>
        <div>
          <div className="display">Team wiki</div>
          <div className="sub">What Comrade knows — every fact citable, every change revertible</div>
        </div>
        {lastCompile?.finished_at && (
          <div className="mono" style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--faint)' }}>
            LAST COMPILED {messageTime(lastCompile.finished_at).toUpperCase()} · Nº{' '}
            {lastCompile.id.slice(0, 4).toUpperCase()}
          </div>
        )}
      </header>
      <div
        style={{
          maxWidth: 720,
          margin: '0 auto',
          padding: '26px 32px 44px',
          display: 'flex',
          flexDirection: 'column',
          gap: 18,
        }}
      >
        {error && <div style={{ fontSize: 12.5, color: 'var(--terracotta)' }}>{error}</div>}
        {pages === null && !error && (
          <div style={{ fontSize: 13, color: 'var(--faint)' }}>Loading…</div>
        )}
        {pages?.length === 0 && (
          <div className="card" style={{ padding: '17px 20px', fontSize: 13, color: 'var(--text-soft)', lineHeight: 1.6 }}>
            Nothing compiled yet. Share documents or talk in the group room — the pipeline builds
            the wiki from there.
          </div>
        )}
        {pages?.map((pg) => (
          <div key={pg.pageId ?? 'orphan'} className="card fade-up" style={{ padding: '18px 21px' }}>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 9 }}>
              <div className="display" style={{ fontSize: 20 }}>
                {pg.title}
              </div>
              {/* A procedure is a different kind of claim from a fact — "how we
                  do this" rather than "this is true" — and a page that renders
                  identically to a fact page has not actually been added
                  (findings §24.2, §6.3-3). */}
              {pg.kind === 'skill' && (
                <span
                  className="mono"
                  style={{
                    fontSize: 9.5,
                    letterSpacing: '0.14em',
                    textTransform: 'uppercase',
                    color: 'var(--muted)',
                    border: '1px solid var(--border-soft)',
                    borderRadius: 3,
                    padding: '2px 6px',
                  }}
                >
                  how we do it
                </span>
              )}
            </div>
            {pg.description && (
              <div style={{ fontSize: 11.5, fontStyle: 'italic', color: 'var(--muted)', marginTop: 4 }}>
                {pg.description}
              </div>
            )}
            <div style={{ display: 'flex', flexDirection: 'column', marginTop: 12 }}>
              {pg.facts.map((f) => (
                <div
                  key={f.entryId}
                  style={{ borderTop: '1px dashed var(--border-soft)', padding: '9px 0' }}
                >
                  <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
                    <span style={{ flex: 'none', color: 'var(--terracotta)', fontWeight: 700, marginTop: 1 }}>
                      —
                    </span>
                    <span style={{ flex: 1, fontSize: 13, lineHeight: 1.5, color: 'var(--text-body)' }}>
                      {f.active.fact}
                      {/* When it became true, and where it came from. The
                          model has had this since findings §20.3.1; the
                          member reading the same wiki had not, so a fact
                          compiled today and one compiled in May looked
                          identical to the person deciding whether to act. */}
                      {factProvenance(f) && (
                        <span
                          style={{
                            marginLeft: 8,
                            fontSize: 11,
                            fontStyle: 'italic',
                            color: 'var(--muted)',
                            whiteSpace: 'nowrap',
                          }}
                        >
                          {/* Leading space so a screen reader does not read
                              "Fridayas of Aug 31" as one word — the margin is
                              visual only. */}
                          {' '}{factProvenance(f)}
                        </span>
                      )}
                      {f.citations.map((c) => (
                        <Link
                          key={c.id}
                          to={c.source_kind === 'message' ? '../room' : '../docs'}
                          title={c.excerpt ?? undefined}
                          className="mono"
                          style={{
                            border: '1px solid rgba(35,33,48,0.18)',
                            color: 'var(--muted)',
                            fontSize: 9,
                            letterSpacing: '0.06em',
                            borderRadius: 2,
                            padding: '2px 7px',
                            marginLeft: 7,
                            verticalAlign: 1,
                            textDecoration: 'none',
                          }}
                        >
                          {citeLabel(c)}
                        </Link>
                      ))}
                    </span>
                    {f.history.length > 0 && (
                      <button
                        onClick={() =>
                          setExpandedFact((x) => (x === f.entryId ? null : f.entryId))
                        }
                        className="mono"
                        style={{
                          flex: 'none',
                          border: 'none',
                          background: 'transparent',
                          color: 'var(--plum)',
                          fontSize: 10,
                          letterSpacing: '0.08em',
                          cursor: 'pointer',
                          padding: '2px 0',
                        }}
                      >
                        {expandedFact === f.entryId ? '▾' : '▸'} {f.history.length} REVISION
                        {f.history.length > 1 ? 'S' : ''}
                      </button>
                    )}
                    {f.revertQueued && (
                      <span
                        className="mono"
                        style={{
                          flex: 'none',
                          fontSize: 9,
                          letterSpacing: '0.08em',
                          color: 'var(--plum)',
                          border: '1px solid rgba(110,95,135,0.4)',
                          borderRadius: 2,
                          padding: '3px 7px',
                        }}
                      >
                        REVERT QUEUED
                      </span>
                    )}
                  </div>
                  {expandedFact === f.entryId && (
                    <div
                      style={{
                        margin: '8px 0 2px 22px',
                        borderLeft: '2px solid rgba(110,95,135,0.35)',
                        padding: '2px 0 2px 13px',
                        display: 'flex',
                        flexDirection: 'column',
                        gap: 7,
                      }}
                    >
                      {f.history.map((h) => (
                        <div
                          key={h.id}
                          style={{ display: 'flex', alignItems: 'center', gap: 10, fontSize: 12 }}
                        >
                          <span style={{ flex: 1, color: 'var(--muted)', textDecoration: 'line-through' }}>
                            {h.fact}
                          </span>
                          <span className="mono" style={{ fontSize: 9, color: 'var(--faint)' }}>
                            {new Date(h.valid_from).toLocaleDateString([], {
                              month: 'short',
                              day: 'numeric',
                            }).toUpperCase()}
                            {h.valid_until
                              ? ` – ${new Date(h.valid_until)
                                  .toLocaleDateString([], { month: 'short', day: 'numeric' })
                                  .toUpperCase()}`
                              : ''}
                          </span>
                          {!f.revertQueued && (
                            <button
                              onClick={() => void queueRevert(f)}
                              className="mono"
                              style={{
                                border: '1px solid rgba(35,33,48,0.25)',
                                background: '#fff',
                                color: 'var(--text-soft)',
                                fontSize: 10,
                                borderRadius: 2,
                                padding: '3px 8px',
                                cursor: 'pointer',
                              }}
                            >
                              ⟲ restore
                            </button>
                          )}
                        </div>
                      ))}
                      <div className="mono" style={{ fontSize: 10, color: 'var(--faint)' }}>
                        RESTORING QUEUES A REVERT — THE PIPELINE RE-COMPILES, IT NEVER SILENTLY EDITS
                      </div>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        ))}
        <div style={{ fontSize: 11, color: 'var(--faint)', lineHeight: 1.6, padding: '0 4px' }}>
          Only the document + chat pipeline writes here — members read, cite, and revert. Chat
          messages can't inject memory directly.
        </div>
      </div>
    </main>
  );
}
