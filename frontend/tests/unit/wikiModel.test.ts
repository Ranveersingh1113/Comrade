import { describe, expect, test } from 'vitest';
import { projectWiki, UNCATEGORIZED } from '../../src/lib/wikiModel';
import type {
  MemoryCitation, MemoryEntry, MemoryPage, MemoryVersion,
} from '../../src/lib/types';

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
