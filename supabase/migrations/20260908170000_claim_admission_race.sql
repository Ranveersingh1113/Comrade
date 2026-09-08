-- Serialize per-team admission, and stop the scan locking rows it will not use.
--
-- 🔴 TWO defects, one statement.
--
-- 1. `claim_next_agent_run` took a per-THREAD advisory lock, which stops two
--    workers taking the same thread. It did nothing about two workers taking
--    DIFFERENT threads of the same team: under READ COMMITTED each statement
--    takes its own snapshot, neither transaction sees the other's uncommitted
--    update, so both counted the same number of running runs, both found room,
--    and both admitted. `for update skip locked` did not help — the two
--    workers were locking different rows, so there was nothing to conflict on.
--    The per-team ceiling was advice under exactly the condition it exists
--    for, and the test meant to cover it claimed SEQUENTIALLY, so the second
--    call always saw the first already committed.
--
-- 2. `pg_try_advisory_xact_lock` sat in the WHERE clause, where it is evaluated
--    as the executor scans and the lock is taken even for rows the scan then
--    rejects. So the first worker to scan acquired thread locks on candidates
--    it never claimed, and every other worker arriving in the same instant
--    found nothing claimable at all. Measured: three workers, three queued
--    runs across two teams, one admitted. The queue was not full — the scan
--    had locked it.
--
-- Now: pick the candidate with row locks only (`for update skip locked`), then
-- take the thread lock on THAT ROW ALONE, then take a blocking advisory lock on
-- the TEAM before the count that decides admission. READ COMMITTED re-snapshots
-- per statement, so the count after acquiring the team lock sees whatever the
-- previous holder committed. Different teams take different locks and stay
-- concurrent.
--
-- The loop matters as much as the locks: a worker whose candidate belongs to a
-- team already at its ceiling must move on to another team's queued work rather
-- than reporting the whole queue empty. The old ceiling filter did that
-- implicitly by living inside the candidate query; checking after selection
-- means doing it explicitly.
create or replace function public.claim_next_agent_run(
  p_worker_id text,
  p_max_per_team integer default 2
)
returns table (
  id uuid, team_id uuid, requester_id uuid, thread_id uuid,
  input_message_id uuid, trigger_type text, attempts integer, worker_id text
) language plpgsql security definer set search_path = '' as $$
declare
  v_id uuid;
  v_team uuid;
  v_thread uuid;
  v_skip uuid[] := '{}';      -- runs this call has looked at and passed over
  v_full uuid[] := '{}';      -- teams found to be at their ceiling
  v_running integer;
  -- How far to look for claimable work before giving up. A claim is a short
  -- transaction on a hot path, and scanning every team in the deployment is a
  -- cost paid on every poll of an otherwise idle queue.
  v_budget integer := 10;
begin
  perform public.recover_expired_agent_runs();

  while v_budget > 0 loop
    v_budget := v_budget - 1;
    v_id := null;

    -- Row locks only. Nothing here takes an advisory lock, so a worker that
    -- ends up claiming nothing has not made the queue look empty to anyone
    -- else.
    select r.id, r.team_id, r.thread_id into v_id, v_team, v_thread
    from public.agent_runs r
    where r.status = 'queued'
      and not (r.id = any(v_skip))
      and not (r.team_id = any(v_full))
      and not exists (
        select 1 from public.agent_runs active
        where active.thread_id = r.thread_id
          and active.status in ('running', 'waiting_for_permission', 'waiting_for_user')
      )
    order by r.created_at
    for update skip locked
    limit 1;

    exit when v_id is null;

    -- One turn at a time per thread. `for update skip locked` already stops two
    -- workers taking the same ROW; this stops them taking two different queued
    -- runs that belong to the same thread.
    if not pg_try_advisory_xact_lock(hashtextextended(v_thread::text, 0)) then
      v_skip := v_skip || v_id;
      continue;
    end if;

    -- Admission for this team, serialised. Blocking rather than `try`: failing
    -- to get it would mean reporting no work while another worker is mid
    -- admission, and this transaction is one update long.
    perform pg_advisory_xact_lock(
      hashtextextended('agent_admission:' || v_team::text, 0)
    );

    -- 'running' only. A run parked on a consent card holds no worker, so
    -- counting it here would let a team with two pending cards lock itself out
    -- of the very turns that would resolve them.
    select count(*) into v_running
    from public.agent_runs busy
    where busy.team_id = v_team and busy.status = 'running';

    if v_running < p_max_per_team then
      return query
      update public.agent_runs r
      set status = 'running', attempts = r.attempts + 1, worker_id = p_worker_id,
          lease_expires_at = now() + interval '5 minutes', finished_at = null
      where r.id = v_id
      returning r.id, r.team_id, r.requester_id, r.thread_id,
                r.input_message_id, r.trigger_type, r.attempts, r.worker_id;
      return;
    end if;

    -- Full. Look past it rather than reporting the queue empty.
    v_full := v_full || v_team;
  end loop;

  return;
end;
$$;

revoke all on function public.claim_next_agent_run(text, integer)
  from public, anon, authenticated;
grant execute on function public.claim_next_agent_run(text, integer)
  to comrade_agent;
