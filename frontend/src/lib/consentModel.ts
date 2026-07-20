// Consent-card state machine. Tiers grade by blast radius (T0-T3); T3 needs
// two keys — the initiator plus any OTHER member (backend enforces; this
// module only decides what the viewer is shown).
import type { ConsentItem } from './types';

export type ConsentPhase =
  | 'pending'               // actionable by the requester
  | 'awaiting_second_key'   // T3: requester approved, no countersign yet
  | 'countersigned_pending' // T3: countersigned, requester not yet approved
  | 'can_countersign'       // T3: viewer is a teammate who can add the key
  | 'executed'
  | 'rejected'
  | 'cancelled'
  | 'stale';

export function consentPhase(
  item: ConsentItem,
  viewerId: string | null,
  staleFromApi: boolean,
): ConsentPhase {
  if (staleFromApi) return 'stale';
  if (item.status === 'executed') return 'executed';
  if (item.status === 'rejected') return 'rejected';
  if (item.status === 'cancelled') return 'cancelled';

  const isRequester = viewerId !== null && item.requesting_member_id === viewerId;
  if (item.tier === 'T3') {
    const countersigned = item.second_approver_id !== null;
    if (item.status === 'approved' && !countersigned) return 'awaiting_second_key';
    if (!isRequester) {
      // Teammate's view: sign it, or watch it wait on the requester.
      return countersigned ? 'countersigned_pending' : 'can_countersign';
    }
    // Requester's view stays actionable — their key still has to turn,
    // countersigned or not.
  }
  return 'pending';
}

/** Literal args, stable order — the card must show EXACTLY what will run. */
export function argsPretty(args: Record<string, unknown>): string {
  const ordered = Object.fromEntries(
    Object.entries(args).sort(([a], [b]) => a.localeCompare(b)),
  );
  return JSON.stringify(ordered, null, 2);
}
