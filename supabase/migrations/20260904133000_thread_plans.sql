-- A thread may have a plan. Most never will: the tool is optional, and a row
-- created with the thread would be an instruction to fill it in.
--
-- One row per thread, versioned. Two runs steering the same thread (steering
-- arrives mid-run, Task 10) would otherwise write over each other's step list
-- with no trace; the version is what makes the second one notice.
--
-- No consent row, no approval: this changes nothing outside the thread. The
-- boundary is RLS, and it is the thread's own — whoever can read the thread
-- can read its plan, and nobody else.
create table public.thread_plans (
  thread_id uuid primary key,
  team_id uuid not null,
  version int not null default 1,
  steps jsonb not null,
  updated_by_kind text not null default 'agent'
    check (updated_by_kind in ('agent', 'user')),
  updated_at timestamptz not null default now(),
  -- Composite, so a plan cannot name one team's thread while claiming
  -- another's team_id. The team_id column is not redundant: current_team() is
  -- what the agent's policy has to compare against, and a policy that had to
  -- join threads to find it would be a policy that reads a table the agent may
  -- not be able to read.
  constraint thread_plans_thread_team_fk foreign key (thread_id, team_id)
    references public.threads (id, team_id) on delete cascade
);

revoke all on public.thread_plans from authenticated;
alter table public.thread_plans enable row level security;

-- Members read; only the agent writes. A member moving work along changes the
-- thread's work_state (the board, Task 14), not the agent's step list.
grant select on public.thread_plans to authenticated;
create policy au_thread_plans_select on public.thread_plans for select to authenticated
  using (public.can_access_thread(thread_id, (select auth.uid())));

grant select, insert, update on public.thread_plans to comrade_agent;
create policy ag_thread_plans on public.thread_plans for all to comrade_agent
  using (team_id = public.current_team())
  with check (team_id = public.current_team());
