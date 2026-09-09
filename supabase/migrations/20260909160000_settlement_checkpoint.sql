-- When a run's own record of what it spent is FINAL.
--
-- 🔴 (fix.md F49, sixth review.) `settle_from_checkpoint` accepted any run that
-- was terminal with no worker, and settled the totals on its row. Those are two
-- different questions, and the second one is the one that matters: is this
-- row's record of what the run spent complete?
--
--   worker claims a run and sends a model request
--   -> its lease expires with the response still in flight
--   -> recover_expired_agent_runs() requeues it: status='queued', worker_id=null
--      and NOTHING has checkpointed the response that has already been paid for
--   -> the member cancels in that interval
--   -> terminal + no worker, so the server settled the STALE checkpoint (zero)
--   -> the paid response arrives, the old worker's exit settlement finds
--      usage_finalized_at already written, and 1,200 real tokens are discarded.
--
-- `pause_for_permission` is the opposite case and the one the fix was built
-- for: it writes the totals in the SAME statement that drops the lease, so a
-- parked run's record IS complete. Nothing distinguished the two.
--
-- THE INVARIANT: a reservation is reconciled from a run's own record only when
-- that record covers everything the run has spent.
alter table public.agent_runs
  add column if not exists usage_checkpoint_at timestamptz;

comment on column public.agent_runs.usage_checkpoint_at is
  'When a worker last recorded its totals AND gave up execution in the same '
  'statement — i.e. when input_tokens/output_tokens became a complete record. '
  'NULL after any claim, because whoever is executing may have spent more than '
  'the row knows. Only a run whose record is complete may be settled by a '
  'caller that did not perform the work.';

-- A CLAIM invalidates any earlier checkpoint. Written as a trigger rather than
-- an edit to claim_next_agent_run, for the reason the sibling trigger gives:
-- the next path that hands a run to a worker would not know it had to.
--
-- The pair reads as one rule. keep_usage_owner remembers WHO ran it when
-- execution is revoked; this forgets WHAT the row knows when execution begins.
create or replace function public.trg_expire_usage_checkpoint()
returns trigger language plpgsql as $$
begin
  if new.worker_id is not null and new.worker_id is distinct from old.worker_id then
    new.usage_checkpoint_at := null;
  end if;
  return new;
end $$;

drop trigger if exists expire_usage_checkpoint on public.agent_runs;
create trigger expire_usage_checkpoint
  before update on public.agent_runs
  for each row execute function public.trg_expire_usage_checkpoint();

-- Backfill: nothing. A run already parked has no marker, so it is treated as a
-- record that may be incomplete and its reservation is held rather than
-- released. Holding a reservation costs the team admission for the rest of the
-- hour; releasing one that was never earned costs them nothing and loses the
-- record of real spend. The safe direction for a cap is to hold.
