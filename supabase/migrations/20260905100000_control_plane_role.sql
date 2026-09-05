-- Cross-team maintenance must not run as the table owner. CONTROL has only
-- the metadata columns required to claim queues and discover maintenance work;
-- it cannot read member text, wiki bodies, documents, consent arguments, or
-- repository files.
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'comrade_control') then
    create role comrade_control nologin noinherit nobypassrls;
  end if;
end $$;

grant usage on schema public, supabase_migrations to comrade_control;
grant select on supabase_migrations.schema_migrations to comrade_control;

-- Pipeline queue: payload is necessary to dispatch a claimed job; no other
-- application table write is available to this role.
grant select (id, team_id, job_type, payload, attempts, status, created_at,
              picked_at, lease_expires_at, finished_at, last_error)
  on public.jobs to comrade_control;
grant update (status, attempts, picked_at, lease_expires_at, finished_at, last_error)
  on public.jobs to comrade_control;
create policy ctl_jobs on public.jobs for all to comrade_control
  using (true) with check (true);

-- Cross-team sweep inputs. Messages deliberately omit `body` and sender ids.
grant select (id) on public.teams to comrade_control;
grant select (id, team_id, visibility) on public.threads to comrade_control;
grant select (team_id, thread_id, sender_kind, deleted_scope, created_at)
  on public.messages to comrade_control;
grant select (team_id, chat_through, status)
  on public.memory_compilations to comrade_control;
create policy ctl_teams on public.teams for select to comrade_control using (true);
create policy ctl_threads on public.threads for select to comrade_control using (true);
create policy ctl_messages on public.messages for select to comrade_control using (true);
create policy ctl_memory_compilations on public.memory_compilations
  for select to comrade_control using (true);

-- Repository and installation routing metadata; GitHub tokens are never stored.
grant select on public.github_repos to comrade_control;
grant select, delete on public.github_installations to comrade_control;
create policy ctl_github_repos on public.github_repos
  for select to comrade_control using (true);
create policy ctl_github_installations on public.github_installations
  for all to comrade_control using (true) with check (true);

-- Readiness needs only queue age/status, never a run prompt or tool step.
grant select (status, created_at) on public.agent_runs to comrade_control;
create policy ctl_agent_runs on public.agent_runs
  for select to comrade_control using (true);
