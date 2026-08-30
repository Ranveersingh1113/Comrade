-- findings §2.2. action_hash is computed at propose time (shared/consent.py)
-- and re-verified at execute time, but nothing stopped a retried turn from
-- writing the same proposal twice. The only index on the table was
-- idx_consent_queue_team_status.
--
-- Scoped to status='pending': a resolved item is history and may legitimately
-- repeat later (the same action proposed again next week is a new decision).
--
-- CONCURRENTLY per the migration policy — consent_queue is an existing table.
-- Run this file outside a transaction.

create unique index concurrently if not exists uq_consent_pending_hash
  on public.consent_queue (team_id, action_hash)
  where status = 'pending';
