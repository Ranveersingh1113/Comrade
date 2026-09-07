-- Pipeline jobs: who holds a claim, and when a retry may be tried again.
--
-- 🔴 `_finish` updated `where id = %s` and nothing else, so a worker whose
-- lease had expired — and whose job another worker had already reclaimed —
-- still stamped its own result over the new claim. Two workers, one job, and
-- the second one's work discarded by the first one's late answer. There was no
-- column to fence on, which is why the fence did not exist.
--
-- 🔴 And a failed job went straight back to `pending`, claimable on the very
-- next iteration, so its three attempts burned in milliseconds against
-- whatever was already broken. Retrying only helps if something has had time
-- to change.
--
-- Both columns are additive with safe defaults, so the running image keeps
-- working through the window between migrating and activating the new one.
alter table public.jobs
  add column if not exists worker_id text,
  add column if not exists available_at timestamptz not null default now();

-- The claim orders by created_at and filters on availability, so this is the
-- index that keeps it from scanning a growing queue.
create index if not exists idx_jobs_claimable
  on public.jobs (available_at, created_at)
  where status = 'pending';

-- Column-level grants, deliberately: `comrade_control` is granted the exact
-- columns it needs and no others, so a new column is invisible to it until
-- someone says otherwise. That is the design working — it is also why adding
-- a column to this table is never only a schema change.
grant select (worker_id, available_at) on public.jobs to comrade_control;
grant update (worker_id, available_at) on public.jobs to comrade_control;
