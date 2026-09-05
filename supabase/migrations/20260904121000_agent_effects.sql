-- A write-capable tool is claimed before it executes and completed before the
-- next model call. A reclaimed run may reuse a completed result but never
-- guesses whether an interrupted effect happened.
create table public.agent_effects (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.agent_runs(id) on delete cascade,
  team_id uuid not null references public.teams(id) on delete cascade,
  effect_key text not null,
  tool text not null,
  args jsonb not null,
  status text not null default 'started' check (status in ('started', 'completed')),
  result jsonb,
  created_at timestamptz not null default now(),
  completed_at timestamptz,
  unique (run_id, effect_key)
);

revoke all on public.agent_effects from authenticated;
alter table public.agent_effects enable row level security;
grant select, insert, update on public.agent_effects to comrade_agent;
create policy ag_agent_effects on public.agent_effects for all to comrade_agent
  using (team_id = public.current_team())
  with check (team_id = public.current_team());
