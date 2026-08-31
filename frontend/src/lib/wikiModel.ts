// Wiki projection: memory rows -> page views. Mirrors pipeline/wiki.py
// (the backend's member-facing renderer) so both surfaces agree on layout.
import { shortDate } from './format';
import type {
  MemoryCitation, MemoryEntry, MemoryPage, MemoryVersion,
} from './types';

export const UNCATEGORIZED = 'Uncategorized'; // mirrors pipeline/wiki.py ORPHAN_TITLE

const SOURCE_LABEL: Record<string, string> = {
  message: 'from chat',
  document: 'from a doc',
  github: 'from the repo',
};

/**
 * When a fact became true, and where it came from — the same annotation
 * pipeline/wiki.py:annotate() puts in front of the model.
 *
 * findings §20.3.1 measured a 39-point temporal-reasoning gap that turns on
 * whether facts reach the reader carrying dates. Phase 0 fixed that for the
 * MODEL and missed the member: this screen fetched valid_from and source_kind
 * and rendered neither, so a fact compiled this morning and one compiled in
 * May looked identical to the person deciding whether to trust it.
 *
 * Returns null when there is nothing to say, so the caller renders no empty
 * parenthetical.
 */
export function factProvenance(fact: WikiFact): string | null {
  const bits: string[] = [];
  const when = fact.active.valid_from ?? fact.active.created_at;
  if (when) bits.push(`as of ${shortDate(when)}`);
  const label = SOURCE_LABEL[fact.citations[0]?.source_kind ?? ''];
  if (label) bits.push(label);
  // ", " and not " · ": shortDate already contains a "·" ("MON · AUG 31"),
  // so a middot separator here would render "as of MON · AUG 31 · from a doc"
  // — three fragments joined by the same mark, two of which are one fact.
  return bits.length > 0 ? bits.join(', ') : null;
}

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
