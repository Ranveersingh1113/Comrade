// Consent-card state machine. Tiers grade by blast radius (T0-T2) and are
// informational only — every item needs exactly one key, the requester's
// (findings §10, owner decision 2026-08-12). This module only decides what
// the viewer is shown; the backend decides what may happen.
import type { ConsentItem } from './types';

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
