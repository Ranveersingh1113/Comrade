-- A team's hourly budget, counted in a row rather than recomputed.
--
-- 🔴 THE HOLE THIS CLOSES. `_check_turn_budget` read `count(*)` and
-- `sum(tokens)` over agent_runs and compared them to the cap. Two requests
-- that read the same snapshot both pass, and there is no statement anywhere
-- that stops the second: the check and the spend are different transactions,
-- so the cap was advisory. On a shared model key that is somebody else's bill.
--
-- One row per (team, hour). The reservation is an UPDATE on that row, which
-- takes a row lock, which is what makes concurrent turns serialise instead of
-- racing. The cap is enforced in the WHERE clause of the same statement that
-- spends it — there is no window between deciding and doing.
create table public.usage_buckets (
  team_id  uuid not null references public.teams(id) on delete cascade,
  bucket   timestamptz not null,
  turns    int not null default 0,
  tokens   bigint not null default 0,
  primary key (team_id, bucket)
);

-- A turn reserves an ESTIMATE before it runs and reconciles the truth after.
-- Reserving nothing until the tokens are known would let a hundred concurrent
-- turns each pass a cap that none of them had yet spent against.
alter table public.agent_runs
  add column if not exists tokens_reserved int,
  add column if not exists usage_finalized_at timestamptz;

revoke all on public.usage_buckets from authenticated;
alter table public.usage_buckets enable row level security;

-- Server-side accounting only. A member never reads or writes this: the cap is
-- enforced for them, not by them, and the row says nothing they need.
grant select, insert, update on public.usage_buckets to comrade_agent;
create policy ag_usage_buckets on public.usage_buckets for all to comrade_agent
  using (team_id = public.current_team())
  with check (team_id = public.current_team());

create index if not exists idx_usage_buckets_bucket
  on public.usage_buckets (bucket);
