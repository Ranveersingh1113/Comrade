-- Who is entitled to settle a run once execution ownership is revoked.
--
-- 🔴 (fix.md F43, third pass.) The settlement fence accepted ANY caller when
-- `worker_id` was NULL, and that is precisely the state cancellation creates.
--
--   old worker loses its lease -> replacement executes and spends 1,200
--   -> a member cancels -> `cancel_run` sets status='cancelled', worker_id=null
--   -> the STALE worker's exit path settles 50 first and wins
--      `usage_finalized_at` -> the replacement's real number is skipped.
--
-- The bucket then permanently records 50 for a turn that cost 1,200, releasing
-- budget the team actually spent. Two other transitions reach the same shape:
-- `recover_expired_agent_runs` past its attempt limit marks a run 'failed' and
-- nulls the worker while that worker may still be exiting, and
-- `pause_for_permission` nulls it for a parked run.
--
-- THE INVARIANT: a run is settled exactly once, by the identity that actually
-- performed the work whose totals are being recorded.
--
-- So the identity is PRESERVED at the moment it is revoked, rather than
-- inferred afterwards from a null. A trigger rather than an edit to each
-- writer: three places null this column today and the next one would not know
-- it had to.
alter table public.agent_runs
  add column if not exists usage_owner text;

comment on column public.agent_runs.usage_owner is
  'The worker that last held execution, kept when worker_id is cleared so '
  'settlement can still be attributed. NULL means nothing ever executed this '
  'run — a queued cancellation — which is a different case from a revoked one.';

create or replace function public.trg_keep_usage_owner()
returns trigger language plpgsql as $$
begin
  -- Only on revocation. A run being CLAIMED (null -> id) is acquiring an
  -- owner, not losing one, and must not overwrite the record of who ran it.
  if old.worker_id is not null and new.worker_id is null then
    new.usage_owner := old.worker_id;
  end if;
  return new;
end $$;

drop trigger if exists keep_usage_owner on public.agent_runs;
create trigger keep_usage_owner
  before update on public.agent_runs
  for each row execute function public.trg_keep_usage_owner();

-- Backfill: a run already in this state has no record of its last owner, and
-- guessing one would be worse than admitting it. They stay NULL, which under
-- the new predicate means only an identity-less caller can settle them — the
-- same treatment as a queued cancellation, and a safe direction for a cap.
