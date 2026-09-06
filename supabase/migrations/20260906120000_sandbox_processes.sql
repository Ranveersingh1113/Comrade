-- A long-running process a thread started, and the row that outlives it.
--
-- `repo_run` is finite: it starts a container, waits, and returns. A
-- development server is not — it must keep running after the turn that started
-- it ends, which means the only record that it exists is this row. Without it
-- a worker restart leaks a container nobody can name, holding a port and a CPU
-- until someone notices the host is slow.
--
-- So the row is written BEFORE the container starts and updated after. A row
-- in `starting` with no container id is a process whose creation was
-- interrupted; the reconciler can find it precisely because we wrote first.
create table public.sandbox_processes (
  id            uuid primary key default gen_random_uuid(),
  team_id       uuid not null references public.teams(id) on delete cascade,
  thread_id     uuid not null,
  agent_run_id  uuid references public.agent_runs(id) on delete set null,
  command       text not null,
  -- The one port that may ever be previewed. Declared when the process starts
  -- and immutable after: a preview token names a port, and a process that
  -- could change its port after a token was minted would let one grant reach a
  -- service the member never approved.
  port          int check (port is null or (port between 1 and 65535)),
  container_id  text,
  state         text not null default 'starting'
                check (state in ('starting','running','exited','failed','stopped','expired')),
  exit_code     int,
  detail        text,
  started_at    timestamptz not null default now(),
  last_seen_at  timestamptz not null default now(),
  stopped_at    timestamptz,
  -- Composite, so a process cannot name one team's thread while claiming
  -- another's team_id — the same rule thread_plans carries.
  constraint sandbox_processes_thread_team_fk foreign key (thread_id, team_id)
    references public.threads (id, team_id) on delete cascade
);

create index idx_sandbox_processes_live on public.sandbox_processes (state, last_seen_at)
  where state in ('starting', 'running');
create index idx_sandbox_processes_thread on public.sandbox_processes (thread_id, started_at desc);

revoke all on public.sandbox_processes from authenticated;
alter table public.sandbox_processes enable row level security;

-- Participants SEE what is running in their thread. They do not start or stop
-- one from the browser: a process belongs to the agent turn that created it,
-- and a stop button that raced a starting container would leave exactly the
-- orphan this table exists to prevent.
grant select on public.sandbox_processes to authenticated;
create policy au_sandbox_processes_select on public.sandbox_processes
  for select to authenticated
  using (public.can_access_thread(thread_id, (select auth.uid())));

grant select, insert, update on public.sandbox_processes to comrade_agent;
create policy ag_sandbox_processes on public.sandbox_processes for all to comrade_agent
  using (team_id = public.current_team())
  with check (team_id = public.current_team());

-- The reconciler runs across every team, so it reads and closes rows the way
-- the queue sweep does — metadata only. It never learns the command's output.
grant select (id, team_id, thread_id, container_id, state, port,
              started_at, last_seen_at, stopped_at) on public.sandbox_processes
  to comrade_control;
grant update (state, exit_code, detail, last_seen_at, stopped_at)
  on public.sandbox_processes to comrade_control;
create policy ctl_sandbox_processes on public.sandbox_processes
  for all to comrade_control using (true) with check (true);
