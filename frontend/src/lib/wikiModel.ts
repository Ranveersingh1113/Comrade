// Wiki projection: memory rows -> page views. Mirrors pipeline/wiki.py
// (the backend's member-facing renderer) so both surfaces agree on layout.
import type {
  MemoryCitation, MemoryEntry, MemoryPage, MemoryVersion,
} from './types';

export const UNCATEGORIZED = 'Uncategorized'; // mirrors pipeline/wiki.py ORPHAN_TITLE

export interface WikiFact {
  entryId: string;
  active: MemoryVersion;
  history: MemoryVersion[]; // inactive, newest first
  citations: MemoryCitation[];
  revertQueued: boolean;
}

export interface WikiPage {
  pageId: string | null;
  title: string;
  description: string;
  facts: WikiFact[];
}

/**
 * Pages in the order given (screen queries title-ordered), Uncategorized
 * last, empty pages dropped. `versions` must be created_at-ascending, as
 * queried. Only unarchived entries with an active version become facts.
 */
export function projectWiki(
  pages: ReadonlyArray<MemoryPage>,
  entries: ReadonlyArray<MemoryEntry>,
  versions: ReadonlyArray<MemoryVersion>,
  citations: ReadonlyArray<MemoryCitation>,
  reverts: ReadonlyArray<{ entry_id: string }>,
): WikiPage[] {
  const citationsByVersion = new Map<string, MemoryCitation[]>();
  for (const c of citations) {
    const list = citationsByVersion.get(c.version_id) ?? [];
    citationsByVersion.set(c.version_id, [...list, c]);
  }
  const revertedEntries = new Set(reverts.map((r) => r.entry_id));

  const factsByEntry = new Map<string, WikiFact>();
  for (const e of entries) {
    if (e.archived) continue;
    const vs = versions.filter((v) => v.entry_id === e.id);
    const active = vs.find((v) => v.is_active);
    if (!active) continue; // fully invalidated -> renders nowhere
    factsByEntry.set(e.id, {
      entryId: e.id,
      active,
      history: vs.filter((v) => !v.is_active).reverse(),
      citations: citationsByVersion.get(active.id) ?? [],
      revertQueued: revertedEntries.has(e.id),
    });
  }

  const collect = (predicate: (e: MemoryEntry) => boolean): WikiFact[] =>
    entries
      .filter(predicate)
      .map((e) => factsByEntry.get(e.id))
      .filter((f): f is WikiFact => f !== undefined);

  const views: WikiPage[] = pages.map((p) => ({
    pageId: p.id,
    title: p.title,
    description: p.description,
    facts: collect((e) => e.page_id === p.id),
  }));
  const orphans = collect((e) => e.page_id === null);
  if (orphans.length > 0) {
    views.push({ pageId: null, title: UNCATEGORIZED, description: '', facts: orphans });
  }
  return views.filter((v) => v.facts.length > 0);
}
