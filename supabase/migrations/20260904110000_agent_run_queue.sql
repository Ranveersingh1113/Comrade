-- Durable agent queue. A queued turn survives an API/worker restart; the
-- worker lease, rather than a process-local advisory lock, owns execution.
alter table public.agent_runs
  add column if not exists attempts integer not null default 0,
  add column if not exists worker_id text,
  add column if not exists lease_expires_at timestamptz,
  add column if not exists last_error text;

-- A pre-queue process could leave a `running` row with no worker or lease.
-- It cannot safely resume after this deploy because its in-memory ADK session
-- is gone; fail it explicitly instead of letting it block its thread forever.
update public.agent_runs
set status = 'failed', finished_at = coalesce(finished_at, now()),
    last_error = coalesce(last_error, 'superseded by durable queue deployment')
where status = 'running' and worker_id is null and lease_expires_at is null;

alter table public.agent_runs drop constraint if exists agent_runs_status_check;
alter table public.agent_runs add constraint agent_runs_status_check check (
  status in (
    'queued', 'running', 'waiting_for_permission', 'waiting_for_user',
    'done', 'failed', 'cancelled'
  )
) not valid;
alter table public.agent_runs validate constraint agent_runs_status_check;

-- A thread may queue many turns but may have only one active turn. The index
-- is also the backstop if a future claimant accidentally bypasses the claim SQL.
create unique index if not exists one_active_agent_run_per_thread
  on public.agent_runs (thread_id)
  where status in ('running', 'waiting_for_permission', 'waiting_for_user');
create index if not exists idx_agent_runs_queue
  on public.agent_runs (created_at) where status = 'queued';

-- The worker needs to select across teams, but it must not use the table-owner
-- connection. These narrow definer functions are its control plane; every
-- normal run/tool write still uses the agent role inside a team session.
create or replace function public.recover_expired_agent_runs()
returns integer language plpgsql security definer set search_path = '' as $$
declare _count integer;
begin
  update public.agent_runs
  set status = case when attempts >= 3 then 'failed' else 'queued' end,
      worker_id = null, lease_expires_at = null,
      finished_at = case when attempts >= 3 then now() else null end,
      last_error = coalesce(last_error, 'worker lease expired')
  where status in ('running', 'waiting_for_permission', 'waiting_for_user')
    and lease_expires_at < now();
  get diagnostics _count = row_count;
  return _count;
end;
$$;

create or replace function public.claim_next_agent_run(p_worker_id text)
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

create or replace function public.renew_agent_run_lease(p_run_id uuid, p_worker_id text)
returns boolean language sql security definer set search_path = '' as $$
  with renewed as (
    update public.agent_runs
    set lease_expires_at = now() + interval '5 minutes'
    where id = p_run_id and worker_id = p_worker_id and status = 'running'
      and lease_expires_at >= now()
    returning 1
  ) select exists (select 1 from renewed);
$$;

revoke all on function public.recover_expired_agent_runs() from public, anon, authenticated;
revoke all on function public.claim_next_agent_run(text) from public, anon, authenticated;
revoke all on function public.renew_agent_run_lease(uuid, text) from public, anon, authenticated;
grant execute on function public.recover_expired_agent_runs(),
  public.claim_next_agent_run(text), public.renew_agent_run_lease(uuid, text)
  to comrade_agent;

-- User messages and their queued run are one transaction. This function is
-- intentionally the only user-facing write path to agent_runs: it checks the
-- caller's authenticated identity and thread access before bypassing RLS.
create or replace function public.enqueue_agent_turn(
  p_team_id uuid,
  p_thread_id uuid,
  p_text text,
  p_trigger_type text default 'user'
) returns table (run_id uuid, message_id uuid)
language plpgsql security definer set search_path = '' as $$
declare
  v_user_id uuid := auth.uid();
begin
  if v_user_id is null
    or not public.can_access_thread(p_thread_id, v_user_id)
    or not exists (
      select 1 from public.threads where id = p_thread_id and team_id = p_team_id
    ) then
    raise exception 'thread not accessible' using errcode = '42501';
  end if;

  insert into public.messages (team_id, thread_id, sender_kind, sender_id, body)
  values (p_team_id, p_thread_id, 'user', v_user_id, p_text)
  returning id into message_id;

  insert into public.agent_runs (
    team_id, requester_id, thread_id, input_message_id, trigger_type,
    input_summary, status
  ) values (
    p_team_id, v_user_id, p_thread_id, message_id, p_trigger_type,
    left(p_text, 200), 'queued'
  ) returning id into run_id;
  return next;
end;
$$;
revoke all on function public.enqueue_agent_turn(uuid, uuid, text, text) from public, anon;
grant execute on function public.enqueue_agent_turn(uuid, uuid, text, text) to authenticated;
