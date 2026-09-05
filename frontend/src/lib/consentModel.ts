// Consent-card state machine. Tiers grade by blast radius (T0-T2) and are
// informational only — every item needs exactly one key, the requester's
// (findings §10, owner decision 2026-08-12). This module only decides what
// the viewer is shown; the backend decides what may happen.
import type { ConsentItem } from './types';

export type ConsentPhase =
  | 'pending' // actionable by the requester
  | 'expired' // past its 7-day backstop; execute_consent would refuse it
  | 'executed'
  | 'rejected'
  | 'cancelled'
  | 'stale';

/**
 * Has this item passed its 7-day backstop?
 *
 * `status` stays 'pending' forever — nothing sweeps the queue — so expiry
 * lives only in `expires_at`. Without this check an expired item sat in the
 * Pending list, counted toward the sidebar's "awaiting key" badge, and offered
 * an APPROVE button that fails: execute_consent re-checks expiry and raises
 * ConsentError, so the member gets a 409 for doing exactly what the screen
 * invited them to do.
 *
 * `now` is injectable so the tests do not depend on the wall clock.
 */
export function isExpired(item: ConsentItem, now: number = Date.now()): boolean {
  return item.expires_at !== null && new Date(item.expires_at).getTime() <= now;
}

/** Actionable right now: pending, and not past its backstop. */
export function isActionable(item: ConsentItem, now: number = Date.now()): boolean {
  return item.status === 'pending' && !isExpired(item, now);
}

export function consentPhase(
  item: ConsentItem,
  _viewerId: string | null,
  staleFromApi: boolean,
  now: number = Date.now(),
): ConsentPhase {
  if (staleFromApi) return 'stale';
  if (item.status === 'executed') return 'executed';
  if (item.status === 'rejected') return 'rejected';
  if (item.status === 'cancelled') return 'cancelled';
  // Expiry is not a status in the database, so it has to be derived here or
  // the card renders an approve button for something that can no longer run.
  if (isExpired(item, now)) return 'expired';
  return 'pending';
}

/** Literal args, stable order — the card must show EXACTLY what will run. */
export function argsPretty(args: Record<string, unknown>): string {
  const ordered = Object.fromEntries(
    Object.entries(args).sort(([a], [b]) => a.localeCompare(b)),
  );
  return JSON.stringify(ordered, null, 2);
}
