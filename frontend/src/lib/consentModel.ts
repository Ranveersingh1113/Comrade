// Consent-card state machine. Tiers grade by blast radius (T0-T2) and are
// informational only — every item needs exactly one key, the requester's
// (findings §10, owner decision 2026-08-12). This module only decides what
// the viewer is shown; the backend decides what may happen.
import type { ConsentItem, ConsentStatus } from './types';

export type ConsentPhase =
  | 'pending' // actionable by the requester
  | 'executed'
  | 'rejected'
  | 'cancelled'
  | 'stale';

export function consentPhase(
  item: ConsentItem,
  _viewerId: string | null,
  staleFromApi: boolean,
): ConsentPhase {
  if (staleFromApi) return 'stale';
  if (item.status === 'executed') return 'executed';
  if (item.status === 'rejected') return 'rejected';
  if (item.status === 'cancelled') return 'cancelled';
  return 'pending';
}

/** Literal args, stable order — the card must show EXACTLY what will run. */
export function argsPretty(args: Record<string, unknown>): string {
  const ordered = Object.fromEntries(
    Object.entries(args).sort(([a], [b]) => a.localeCompare(b)),
  );
  return JSON.stringify(ordered, null, 2);
}

// ---------------------------------------------------------------------------
// Batch grouping (task 6): a DISPLAY concern only. propose_batch stamps one
// batch_id across several proposals so the inbox can show them under one
// heading with progress — it is never an approval gate. Every item here still
// renders its own ConsentCard with its own approve/reject; nothing in this
// module resolves more than one item at a time.
// ---------------------------------------------------------------------------

const APPROVED_STATUSES: ReadonlySet<ConsentStatus> = new Set([
  'executed', 'approved', 'edited',
]);

/** "2 of 5 approved" — counted over the WHOLE batch, including members that
 * resolved already and no longer appear in the pending list. */
export function batchProgressLabel(batchItems: ConsentItem[]): string {
  const approved = batchItems.filter((i) => APPROVED_STATUSES.has(i.status)).length;
  return `${approved} of ${batchItems.length} approved`;
}

export type PendingRow =
  | { kind: 'single'; item: ConsentItem }
  | {
      kind: 'batch';
      batchId: string;
      items: ConsentItem[]; // the still-pending members only — one card each
      totalCount: number;
      progressLabel: string;
    };

/**
 * Lays out the pending queue: an item with no batch_id is its own row,
 * unchanged from before batching existed. Items sharing a batch_id collapse
 * into one row so they render under one heading instead of as unrelated
 * cards (findings §5's consent-fatigue problem) — but that row still carries
 * one ConsentCard per still-pending member, each independently actionable.
 *
 * `allItems` (every status) feeds the progress count; `pendingItems` (already
 * filtered + ordered by the caller, same as today) drives what actually gets
 * a row. A batch with nothing left pending simply produces no row — its
 * resolved members already show in the history list, unchanged.
 */
export function pendingQueueRows(
  allItems: ConsentItem[],
  pendingItems: ConsentItem[],
): PendingRow[] {
  const batches = new Map<string, ConsentItem[]>();
  for (const item of allItems) {
    if (!item.batch_id) continue;
    const list = batches.get(item.batch_id);
    if (list) list.push(item);
    else batches.set(item.batch_id, [item]);
  }

  const rows: PendingRow[] = [];
  const rendered = new Set<string>();
  for (const item of pendingItems) {
    if (!item.batch_id) {
      rows.push({ kind: 'single', item });
      continue;
    }
    if (rendered.has(item.batch_id)) continue;
    rendered.add(item.batch_id);
    const batchItems = batches.get(item.batch_id) ?? [item];
    rows.push({
      kind: 'batch',
      batchId: item.batch_id,
      items: batchItems.filter((i) => i.status === 'pending'),
      totalCount: batchItems.length,
      progressLabel: batchProgressLabel(batchItems),
    });
  }
  return rows;
}
