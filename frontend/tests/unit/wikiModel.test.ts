import { describe, expect, it, test } from 'vitest';
import { factProvenance, projectWiki, UNCATEGORIZED } from '../../src/lib/wikiModel';
import type {
  MemoryCitation, MemoryEntry, MemoryPage, MemoryVersion,
} from '../../src/lib/types';
import type { WikiFact } from '../../src/lib/wikiModel';

const page = (id: string, title: string): MemoryPage =>
  ({ id, title, description: '', team_id: 't1' }) as MemoryPage;
const entry = (id: string, page_id: string | null): MemoryEntry =>
  ({ id, page_id, team_id: 't1', archived: false }) as MemoryEntry;
const ver = (
  id: string, entry_id: string, fact: string, is_active: boolean, created_at: string,
): MemoryVersion =>
  ({ id, entry_id, fact, is_active, created_at, change_type: 'added' }) as MemoryVersion;

test('facts group under their pages; page-less land in Uncategorized last', () => {
  const out = projectWiki(
    [page('p1', 'Deadlines')],
    [entry('e1', 'p1'), entry('e2', null)],
    [ver('v1', 'e1', 'Demo Friday', true, '1'), ver('v2', 'e2', 'Stray fact', true, '1')],
    [], [],
  );
  expect(out.map((p) => p.title)).toEqual(['Deadlines', UNCATEGORIZED]);
  expect(out[0].facts[0].active.fact).toBe('Demo Friday');
  expect(out[1].facts[0].active.fact).toBe('Stray fact');
});

test('empty pages are dropped; only ACTIVE versions become facts', () => {
  const out = projectWiki(
    [page('p1', 'Deadlines'), page('p2', 'Empty Page')],
    [entry('e1', 'p1')],
    [ver('v1', 'e1', 'Old', false, '1'), ver('v2', 'e1', 'Current', true, '2')],
    [], [],
  );
  expect(out).toHaveLength(1);
  expect(out[0].facts).toHaveLength(1);
  expect(out[0].facts[0].active.fact).toBe('Current');
});

test('an entry with no active version (fully invalidated) renders nowhere', () => {
  const out = projectWiki(
    [page('p1', 'P')], [entry('e1', 'p1')],
    [ver('v1', 'e1', 'Tombstoned', false, '1')],
    [], [],
  );
  expect(out).toEqual([]);
});

test('history: inactive versions newest-first; citations attach to the active version', () => {
  const cite = { id: 'c1', version_id: 'v3', source_kind: 'message', source_id: 'm1' } as MemoryCitation;
  const out = projectWiki(
    [page('p1', 'P')], [entry('e1', 'p1')],
    // versions arrive created_at-ascending, as the screen queries them
    [ver('v1', 'e1', 'a', false, '1'), ver('v2', 'e1', 'b', false, '2'), ver('v3', 'e1', 'c', true, '3')],
    [cite], [],
  );
  const f = out[0].facts[0];
  expect(f.history.map((h) => h.fact)).toEqual(['b', 'a']);
  expect(f.citations).toEqual([cite]);
});

test('a revert row marks the fact revertQueued', () => {
  const out = projectWiki(
    [page('p1', 'P')], [entry('e1', 'p1')],
    [ver('v1', 'e1', 'x', true, '1')],
    [], [{ entry_id: 'e1' }],
  );
  expect(out[0].facts[0].revertQueued).toBe(true);
});

test('archived entries are excluded even with an active version', () => {
  const archived = { ...entry('e1', 'p1'), archived: true };
  const out = projectWiki(
    [page('p1', 'P')], [archived],
    [ver('v1', 'e1', 'hidden', true, '1')],
    [], [],
  );
  expect(out).toEqual([]);
});

describe('factProvenance', () => {
  const fact = (over: Partial<WikiFact> = {}): WikiFact => ({
    entryId: 'e1',
    active: {
      id: 'v1', entry_id: 'e1', team_id: 't1', compilation_id: null,
      fact: 'The demo is Friday', change_type: 'added', is_active: true,
      valid_from: '2026-08-20T10:00:00Z', valid_until: null,
      created_at: '2026-08-20T10:00:00Z',
    },
    history: [],
    citations: [],
    revertQueued: false,
    ...over,
  });

  it('dates a fact so a stale one is distinguishable from a fresh one', () => {
    expect(factProvenance(fact())).toContain('as of');
  });

  it('names where the fact came from', () => {
    const withDoc = fact({
      citations: [{
        id: 'c1', version_id: 'v1', source_kind: 'document',
        source_id: 'd1', excerpt: null, created_at: '2026-08-20T10:00:00Z',
      }],
    });
    expect(factProvenance(withDoc)).toContain('from a doc');
  });

  it('labels each source kind the compiler can produce', () => {
    for (const [kind, label] of [
      ['message', 'from chat'], ['document', 'from a doc'], ['github', 'from the repo'],
    ] as const) {
      const f = fact({
        citations: [{
          id: 'c1', version_id: 'v1', source_kind: kind,
          source_id: 's1', excerpt: null, created_at: '2026-08-20T10:00:00Z',
        }],
      });
      expect(factProvenance(f)).toContain(label);
    }
  });

  it('renders nothing rather than an empty parenthetical', () => {
    const undated = fact({
      active: { ...fact().active, valid_from: '', created_at: '' },
    });
    expect(factProvenance(undated)).toBeNull();
  });
});

describe('page kind', () => {
  it('carries a skill page through to the view', () => {
    const pages = [
      { id: 'p1', team_id: 't', title: 'Releasing', description: 'how we ship',
        kind: 'skill' as const, created_at: '', updated_at: '' },
    ];
    const entries = [{ id: 'e1', team_id: 't', page_id: 'p1', archived: false }];
    const versions = [
      { id: 'v1', entry_id: 'e1', team_id: 't', fact: 'run the migration first',
        change_type: 'added', is_active: true, valid_from: null, valid_until: null,
        compilation_id: null, created_at: '' },
    ];
    const [page] = projectWiki(pages as never, entries as never, versions as never, [], []);
    expect(page.kind).toBe('skill');
  });

  it('treats a page written before the column existed as a fact page', () => {
    // Rows predating the migration come back without `kind`; undefined would
    // render as neither kind and the badge logic would silently do nothing.
    const pages = [
      { id: 'p1', team_id: 't', title: 'Deadlines', description: '',
        created_at: '', updated_at: '' },
    ];
    const entries = [{ id: 'e1', team_id: 't', page_id: 'p1', archived: false }];
    const versions = [
      { id: 'v1', entry_id: 'e1', team_id: 't', fact: 'demo is 14 march',
        change_type: 'added', is_active: true, valid_from: null, valid_until: null,
        compilation_id: null, created_at: '' },
    ];
    const [page] = projectWiki(pages as never, entries as never, versions as never, [], []);
    expect(page.kind).toBe('fact');
  });

  it('never calls the orphan bucket a procedure', () => {
    // Facts with no page are facts. Mirrors pipeline/wiki.py's ORPHAN_TITLE
    // bucket, which makes the same claim on the backend side.
    const entries = [{ id: 'e1', team_id: 't', page_id: null, archived: false }];
    const versions = [
      { id: 'v1', entry_id: 'e1', team_id: 't', fact: 'a homeless fact',
        change_type: 'added', is_active: true, valid_from: null, valid_until: null,
        compilation_id: null, created_at: '' },
    ];
    const [page] = projectWiki([], entries as never, versions as never, [], []);
    expect(page.title).toBe(UNCATEGORIZED);
    expect(page.kind).toBe('fact');
  });
});
