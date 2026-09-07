-- Ownership evidence that outlives the thread it belonged to.
--
-- 🔴 `sandbox_processes` cascades from threads and teams, which is right for
-- the row and wrong for the CONTAINER. Deleting a thread removed the only
-- record of a running container's name — so the container kept running, on a
-- network nobody could name, and no reconciler could ever find it. The cascade
-- turned a tidy delete into a permanent leak.
--
-- A row here is written BEFORE the cascade takes the process row away, so the
-- evidence needed to clean up survives the thing that owned it.
create table public.sandbox_cleanup (
  id             uuid primary key default gen_random_uuid(),
  container_id   text,
  container_name text,
  network        text,
  reason         text not null,
  requested_at   timestamptz not null default now(),
  attempts       int not null default 0,
  last_error     text,
  done_at        timestamptz
);

create index idx_sandbox_cleanup_pending on public.sandbox_cleanup (requested_at)
  where done_at is null;

create or replace function public.trg_sandbox_process_cleanup()
returns trigger language plpgsql security definer set search_path = '' as $$
begin
  -- Only what actually needs reclaiming. A process that already stopped has
  -- nothing running, and queueing it would make the reconciler chase ghosts.
  if old.container_id is not null and old.state in ('starting', 'running') then
    insert into public.sandbox_cleanup
      (container_id, container_name, network, reason)
    values (old.container_id, old.container_name,
            'comrade-prev-' || replace(old.id::text, '-', ''),
            'owning thread or team was deleted');
  end if;
  return old;
end;
$$;

create trigger trg_sandbox_process_cleanup
  before delete on public.sandbox_processes
  for each row execute function public.trg_sandbox_process_cleanup();

revoke all on public.sandbox_cleanup from authenticated;
alter table public.sandbox_cleanup enable row level security;

-- Cross-team reclamation, so the control role owns it. It holds no member
-- content: a container id, a network name, and why.
grant select, insert, update on public.sandbox_cleanup to comrade_control;
create policy ctl_sandbox_cleanup on public.sandbox_cleanup
  for all to comrade_control using (true) with check (true);
grant insert on public.sandbox_cleanup to comrade_agent;
create policy ag_sandbox_cleanup on public.sandbox_cleanup
  for insert to comrade_agent with check (true);
