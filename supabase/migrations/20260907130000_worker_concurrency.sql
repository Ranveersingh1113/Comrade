-- A per-team ceiling on how many turns run at once.
--
-- 🔴 The worker ran one turn at a time, so a long turn anywhere in the
-- deployment made every other team wait behind it. Nothing in the queue
-- required that: `claim_next_agent_run` already guarantees one active run per
-- THREAD, which is the ordering guarantee that actually matters. The
-- serialisation was in the worker loop alone.
--
-- Lifting it needs this: concurrency without a per-team ceiling is a bigger
-- blast radius rather than more throughput, because one team queueing five
-- turns takes every slot. The ceiling travels INSIDE the claim — two workers
-- asking at the same moment must not both see a team one under its limit.
--
-- The new parameter is defaulted, so the previous one-argument call still
-- resolves during the window between migrating and activating the new image.
create or replace function public.claim_next_agent_run(
  p_worker_id text,
  p_max_per_team integer default 2
)
returns table (
  id uuid, team_id uuid, requester_id uuid, thread_id uuid,
  input_message_id uuid, trigger_type text, attempts integer, worker_id text
) language plpgsql security definer set search_path = '' as $$
begin
  perform public.recover_expired_agent_runs();
  return query
  with candidate as (
    select r.id
    from public.agent_runs r
    where r.status = 'queued'
      -- This transaction-scoped lock eliminates the otherwise-racy case
      -- where two workers see two queued rows for one thread at once.
      and pg_try_advisory_xact_lock(hashtextextended(r.thread_id::text, 0))
      and not exists (
        select 1 from public.agent_runs active
        where active.thread_id = r.thread_id
          and active.status in ('running', 'waiting_for_permission', 'waiting_for_user')
      )
      -- 'running' only. A run parked on a consent card holds no worker, so
      -- counting it here would let a team with two pending cards lock itself
      -- out of the very turns that would resolve them.
      and (
        select count(*) from public.agent_runs busy
        where busy.team_id = r.team_id and busy.status = 'running'
      ) < p_max_per_team
    order by r.created_at
    for update skip locked
    limit 1
  )
  update public.agent_runs r
  set status = 'running', attempts = r.attempts + 1, worker_id = p_worker_id,
      lease_expires_at = now() + interval '5 minutes', finished_at = null
  where r.id = (select candidate.id from candidate)
  returning r.id, r.team_id, r.requester_id, r.thread_id,
            r.input_message_id, r.trigger_type, r.attempts, r.worker_id;
end;
$$;

revoke all on function public.claim_next_agent_run(text, integer)
  from public, anon, authenticated;
grant execute on function public.claim_next_agent_run(text, integer) to comrade_agent;
