-- findings §10, owner decision 2026-08-12. T3 and the two-key countersign are
-- removed. This reverses the provisional governance ruling in
-- 20260719130000_consent_tiers.sql.
--
-- Rationale, narrower than "two-key was overhead" (§24.1): for code, GitHub
-- branch protection is a stronger second key than this trigger, enforced by
-- the system that owns the resource (§16.2). The non-code case is left
-- uncovered, knowingly.
--
-- `tier` SURVIVES, narrowed to T0-T2 (decision Q1). It stays meaningful for
-- _TOOL_TIER_FLOORS and seeds the earned-trust ratchet (§9.3 G4/G5).
-- Narrowing a check is cheaper to reverse than dropping a column.

drop policy if exists au_consent_queue_select_t3 on public.consent_queue;
drop policy if exists au_consent_queue_second_key on public.consent_queue;

drop trigger if exists trg_consent_second_key on public.consent_queue;
drop function if exists public.trg_consent_second_key_guard();

alter table public.consent_queue
  drop constraint if exists consent_second_key_distinct;

alter table public.consent_queue
  drop column if exists second_approver_id,
  drop column if exists second_approved_at;

-- Narrow the tier check. NOT VALID then VALIDATE per the migration policy;
-- any existing T3 row must be rewritten first or VALIDATE will fail.
update public.consent_queue set tier = 'T2' where tier = 'T3';

alter table public.consent_queue
  drop constraint if exists consent_queue_tier_check;
alter table public.consent_queue
  add constraint consent_queue_tier_check
  check (tier in ('T0','T1','T2')) not valid;
alter table public.consent_queue
  validate constraint consent_queue_tier_check;
