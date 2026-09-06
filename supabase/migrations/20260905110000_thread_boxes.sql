-- A Box is execution state for exactly one conversation thread. It has no
-- credential or preview token columns: those stay in the deployment secret store.
create table public.thread_boxes (
  thread_id              uuid primary key,
  team_id                uuid not null,
  box_id                 text not null unique,
  state                  text not null check (state in ('provisioning', 'ready', 'running', 'stopped', 'error')),
  detached_process_id    text,
  source_fingerprint     text,
  dependency_fingerprint text,
  created_at             timestamptz not null default now(),
  last_active_at         timestamptz not null default now(),
  stopped_at             timestamptz,
  updated_at             timestamptz not null default now(),
  foreign key (thread_id, team_id) references public.threads (id, team_id) on delete cascade
);

alter table public.thread_boxes enable row level security;

-- Browser users need neither sandbox IDs nor lifecycle metadata. Control-plane
-- jobs own these rows; the API itself never grants Box capability to a member.
grant select, insert, update on public.thread_boxes to comrade_control;
create policy ctl_thread_boxes on public.thread_boxes for all to comrade_control
  using (true) with check (true);
