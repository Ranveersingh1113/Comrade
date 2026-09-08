-- Workers say they are alive, rather than being inferred from work arriving.
--
-- 🔴 THE DEFECT (fix.md F33). `/ready` decided a deployment could serve a turn
-- from queue AGE: runs queued longer than ten minutes, jobs pending longer than
-- ten minutes. On a quiet deployment both queues are empty, so a stack with
-- BOTH WORKERS STOPPED reported itself ready — indistinguishable from a
-- healthy idle one. The first member to send a message discovers the
-- difference, and readiness is exactly the check that was supposed to prevent
-- that.
--
-- Queue age answers "is work stuck", which is a different question from "is
-- anybody here". A worker has to say so itself, on its own clock, whether or
-- not there is anything to do.
--
-- `capabilities` carries what the worker can actually DO — which sandbox
-- provider it is configured for and whether that provider answered — because
-- the API deliberately has no Docker socket and cannot find that out for
-- itself. A capability probe belongs on the process that holds the socket.
create table if not exists public.worker_heartbeats (
  -- One row per worker kind per host process. `worker_id` is host:pid, the
  -- same identity the queues fence on.
  worker_kind   text not null check (worker_kind in ('agent', 'pipeline')),
  worker_id     text not null,
  last_seen_at  timestamptz not null default now(),
  started_at    timestamptz not null default now(),
  capabilities  jsonb not null default '{}'::jsonb,
  primary key (worker_kind, worker_id)
);

create index if not exists idx_worker_heartbeats_seen
  on public.worker_heartbeats (worker_kind, last_seen_at desc);

alter table public.worker_heartbeats enable row level security;

-- Operational metadata about the deployment, not about any team. No policy for
-- `authenticated`: a member has no business knowing the host names of the
-- processes serving them.
grant select on public.worker_heartbeats to comrade_control;
grant select, insert, update on public.worker_heartbeats to comrade_control;
grant select, insert, update on public.worker_heartbeats to comrade_agent;

drop policy if exists ctl_worker_heartbeats on public.worker_heartbeats;
create policy ctl_worker_heartbeats on public.worker_heartbeats
  for all to comrade_control using (true) with check (true);

drop policy if exists ag_worker_heartbeats on public.worker_heartbeats;
create policy ag_worker_heartbeats on public.worker_heartbeats
  for all to comrade_agent using (true) with check (true);
