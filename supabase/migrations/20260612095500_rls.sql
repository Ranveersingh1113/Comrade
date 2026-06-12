-- Comrade — Row-Level Security
-- Principle: NO identity ever uses service_role at runtime. End users run as
-- `authenticated` (scoped by auth.uid()); backend workers run as custom roles
-- (scoped by SET LOCAL app.current_team_id). RLS enforced for all of them.

-- ============================================================
-- 1. Worker roles (group roles; each environment creates a LOGIN role
--    that is a member of these, with a password kept OUT of version control:
--      create role comrade_agent_local login password '<secret>';
--      grant comrade_agent to comrade_agent_local;
--    Policies/grants below target the GROUP role and apply to its members.)
-- ============================================================
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'comrade_agent') then
    create role comrade_agent nologin nobypassrls;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'comrade_pipeline') then
    create role comrade_pipeline nologin nobypassrls;
  end if;
end $$;

grant usage on schema public to comrade_agent, comrade_pipeline;

-- ============================================================
-- 2. Helper functions (SECURITY DEFINER bypasses RLS for the lookup,
--    preventing infinite recursion on memberships policies).
--    search_path = '' + fully-qualified names = no search_path hijack.
-- ============================================================
create or replace function public.is_team_member(_team_id uuid)
returns boolean language sql stable security definer set search_path = '' as $$
  select exists (
    select 1 from public.memberships
    where team_id = _team_id and user_id = (select auth.uid()) and status = 'active'
  );
$$;

create or replace function public.is_team_leader(_team_id uuid)
returns boolean language sql stable security definer set search_path = '' as $$
  select exists (
    select 1 from public.memberships
    where team_id = _team_id and user_id = (select auth.uid())
      and status = 'active' and role = 'leader'
  );
$$;

create or replace function public.shares_team(_other uuid)
returns boolean language sql stable security definer set search_path = '' as $$
  select exists (
    select 1
    from public.memberships a
    join public.memberships b on a.team_id = b.team_id
    where a.user_id = (select auth.uid()) and a.status = 'active'
      and b.user_id = _other and b.status = 'active'
  );
$$;

-- worker team scope: the team set for the current transaction.
create or replace function public.current_team()
returns uuid language sql stable as $$
  select nullif(current_setting('app.current_team_id', true), '')::uuid;
$$;

grant execute on function public.is_team_member(uuid), public.is_team_leader(uuid),
  public.shares_team(uuid), public.current_team()
  to authenticated, comrade_agent, comrade_pipeline;

-- ============================================================
-- 3. Enable RLS on every table (default-deny once enabled)
-- ============================================================
alter table public.profiles            enable row level security;
alter table public.teams               enable row level security;
alter table public.memberships         enable row level security;
alter table public.messages            enable row level security;
alter table public.documents           enable row level security;
alter table public.document_opens      enable row level security;
alter table public.memory_compilations enable row level security;
alter table public.memory_entries      enable row level security;
alter table public.memory_versions     enable row level security;
alter table public.memory_citations    enable row level security;
alter table public.memory_reverts      enable row level security;
alter table public.tasks               enable row level security;
alter table public.milestones          enable row level security;
alter table public.github_repos        enable row level security;
alter table public.github_activity     enable row level security;
alter table public.consent_queue       enable row level security;
alter table public.agent_runs          enable row level security;
alter table public.jobs                enable row level security;
alter table public.change_log          enable row level security;

-- ============================================================
-- 4. authenticated (end-user) policies — the strict boundary
-- ============================================================

-- profiles: self or someone you share a team with
create policy au_profiles_select on public.profiles for select to authenticated
  using (id = (select auth.uid()) or public.shares_team(id));
create policy au_profiles_insert on public.profiles for insert to authenticated
  with check (id = (select auth.uid()));
create policy au_profiles_update on public.profiles for update to authenticated
  using (id = (select auth.uid())) with check (id = (select auth.uid()));

-- teams: members read; leader renames/deletes; anyone may create (as creator)
create policy au_teams_select on public.teams for select to authenticated
  using (public.is_team_member(id));
create policy au_teams_insert on public.teams for insert to authenticated
  with check (created_by = (select auth.uid()));
create policy au_teams_update on public.teams for update to authenticated
  using (public.is_team_leader(id)) with check (public.is_team_leader(id));
create policy au_teams_delete on public.teams for delete to authenticated
  using (public.is_team_leader(id));

-- memberships: members see roster; leader invites/removes; self accepts own
create policy au_memberships_select on public.memberships for select to authenticated
  using (public.is_team_member(team_id));
create policy au_memberships_insert on public.memberships for insert to authenticated
  with check (user_id = (select auth.uid()) or public.is_team_leader(team_id));
create policy au_memberships_update on public.memberships for update to authenticated
  using (user_id = (select auth.uid()) or public.is_team_leader(team_id))
  with check (user_id = (select auth.uid()) or public.is_team_leader(team_id));
create policy au_memberships_delete on public.memberships for delete to authenticated
  using (public.is_team_leader(team_id));

-- messages: group visible to members; private visible to owner only (leaders blocked)
create policy au_messages_select on public.messages for select to authenticated
  using (public.is_team_member(team_id)
         and (thread_type = 'group' or thread_owner_id = (select auth.uid())));
create policy au_messages_insert on public.messages for insert to authenticated
  with check (public.is_team_member(team_id)
              and sender_kind = 'user' and sender_id = (select auth.uid())
              and (thread_type = 'group' or thread_owner_id = (select auth.uid())));
create policy au_messages_update on public.messages for update to authenticated
  using (sender_id = (select auth.uid())) with check (sender_id = (select auth.uid()));

-- documents: members read; member uploads own; any member edits/soft-deletes (team content)
create policy au_documents_select on public.documents for select to authenticated
  using (public.is_team_member(team_id));
create policy au_documents_insert on public.documents for insert to authenticated
  with check (public.is_team_member(team_id) and uploader_id = (select auth.uid()));
create policy au_documents_update on public.documents for update to authenticated
  using (public.is_team_member(team_id)) with check (public.is_team_member(team_id));

-- document_opens: your own open-state only
create policy au_document_opens_select on public.document_opens for select to authenticated
  using (user_id = (select auth.uid()));
create policy au_document_opens_insert on public.document_opens for insert to authenticated
  with check (user_id = (select auth.uid()));
create policy au_document_opens_update on public.document_opens for update to authenticated
  using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));

-- memory: members READ the compiled artifact; nobody authenticated writes facts
create policy au_memory_compilations_select on public.memory_compilations for select to authenticated
  using (public.is_team_member(team_id));
create policy au_memory_entries_select on public.memory_entries for select to authenticated
  using (public.is_team_member(team_id));
create policy au_memory_versions_select on public.memory_versions for select to authenticated
  using (public.is_team_member(team_id));
create policy au_memory_citations_select on public.memory_citations for select to authenticated
  using (exists (select 1 from public.memory_versions v
                 where v.id = version_id and public.is_team_member(v.team_id)));

-- memory_reverts: members see them and may trigger their own (one-tap revert)
create policy au_memory_reverts_select on public.memory_reverts for select to authenticated
  using (public.is_team_member(team_id));
create policy au_memory_reverts_insert on public.memory_reverts for insert to authenticated
  with check (public.is_team_member(team_id) and member_id = (select auth.uid()));

-- tasks: any member creates/edits; assignee-confirm invariant enforced later (trigger)
create policy au_tasks_select on public.tasks for select to authenticated
  using (public.is_team_member(team_id));
create policy au_tasks_insert on public.tasks for insert to authenticated
  with check (public.is_team_member(team_id));
create policy au_tasks_update on public.tasks for update to authenticated
  using (public.is_team_member(team_id)) with check (public.is_team_member(team_id));

-- milestones: any member (team content) + history
create policy au_milestones_select on public.milestones for select to authenticated
  using (public.is_team_member(team_id));
create policy au_milestones_insert on public.milestones for insert to authenticated
  with check (public.is_team_member(team_id));
create policy au_milestones_update on public.milestones for update to authenticated
  using (public.is_team_member(team_id)) with check (public.is_team_member(team_id));
create policy au_milestones_delete on public.milestones for delete to authenticated
  using (public.is_team_member(team_id));

-- github_repos: members read; connecting/removing a repo = leader (integration config)
create policy au_github_repos_select on public.github_repos for select to authenticated
  using (public.is_team_member(team_id));
create policy au_github_repos_insert on public.github_repos for insert to authenticated
  with check (public.is_team_leader(team_id));
create policy au_github_repos_update on public.github_repos for update to authenticated
  using (public.is_team_leader(team_id)) with check (public.is_team_leader(team_id));
create policy au_github_repos_delete on public.github_repos for delete to authenticated
  using (public.is_team_leader(team_id));

-- github_activity: members read; writes are webhook/pipeline only
create policy au_github_activity_select on public.github_activity for select to authenticated
  using (public.is_team_member(team_id));

-- consent_queue: only the requesting member sees/acts on their own items
create policy au_consent_queue_select on public.consent_queue for select to authenticated
  using (requesting_member_id = (select auth.uid()));
create policy au_consent_queue_update on public.consent_queue for update to authenticated
  using (requesting_member_id = (select auth.uid()))
  with check (requesting_member_id = (select auth.uid()));

-- agent_runs: readable to members (not surfaced loudly); no user writes
create policy au_agent_runs_select on public.agent_runs for select to authenticated
  using (public.is_team_member(team_id));

-- change_log: members read the change history; writes are worker/trigger only
create policy au_change_log_select on public.change_log for select to authenticated
  using (public.is_team_member(team_id));

-- jobs: NO authenticated access (backend queue). RLS on + no policy = denied.

-- ============================================================
-- 5. Worker role grants (capability) + team-scoped policies (rows).
--    GRANTs decide WHICH commands; current_team() decides WHICH rows.
--    comrade_agent is deliberately granted NO writes on memory_* tables.
-- ============================================================

-- ---- comrade_agent: conversational agent + platform MCP server ----
grant select on
  public.profiles, public.teams, public.memberships, public.messages,
  public.documents, public.document_opens,
  public.memory_compilations, public.memory_entries, public.memory_versions,
  public.memory_citations, public.memory_reverts,
  public.tasks, public.milestones, public.github_repos, public.github_activity,
  public.consent_queue, public.agent_runs, public.change_log
to comrade_agent;
grant insert, update on
  public.messages, public.tasks, public.milestones,
  public.consent_queue, public.agent_runs, public.change_log
to comrade_agent;

-- agent policies (team-scoped). for all = one policy covers select/insert/update;
-- unwritable tables are limited by the GRANTs above, not the policy.
create policy ag_profiles on public.profiles for all to comrade_agent
  using (exists (select 1 from public.memberships mm
                 where mm.user_id = id and mm.team_id = public.current_team()))
  with check (exists (select 1 from public.memberships mm
                 where mm.user_id = id and mm.team_id = public.current_team()));
create policy ag_teams on public.teams for all to comrade_agent
  using (id = public.current_team()) with check (id = public.current_team());
create policy ag_memberships on public.memberships for all to comrade_agent
  using (team_id = public.current_team()) with check (team_id = public.current_team());
create policy ag_messages on public.messages for all to comrade_agent
  using (team_id = public.current_team()) with check (team_id = public.current_team());
create policy ag_documents on public.documents for all to comrade_agent
  using (team_id = public.current_team()) with check (team_id = public.current_team());
create policy ag_document_opens on public.document_opens for all to comrade_agent
  using (exists (select 1 from public.documents d
                 where d.id = document_id and d.team_id = public.current_team()))
  with check (exists (select 1 from public.documents d
                 where d.id = document_id and d.team_id = public.current_team()));
create policy ag_memory_compilations on public.memory_compilations for all to comrade_agent
  using (team_id = public.current_team()) with check (team_id = public.current_team());
create policy ag_memory_entries on public.memory_entries for all to comrade_agent
  using (team_id = public.current_team()) with check (team_id = public.current_team());
create policy ag_memory_versions on public.memory_versions for all to comrade_agent
  using (team_id = public.current_team()) with check (team_id = public.current_team());
create policy ag_memory_citations on public.memory_citations for all to comrade_agent
  using (exists (select 1 from public.memory_versions v
                 where v.id = version_id and v.team_id = public.current_team()))
  with check (exists (select 1 from public.memory_versions v
                 where v.id = version_id and v.team_id = public.current_team()));
create policy ag_memory_reverts on public.memory_reverts for all to comrade_agent
  using (team_id = public.current_team()) with check (team_id = public.current_team());
create policy ag_tasks on public.tasks for all to comrade_agent
  using (team_id = public.current_team()) with check (team_id = public.current_team());
create policy ag_milestones on public.milestones for all to comrade_agent
  using (team_id = public.current_team()) with check (team_id = public.current_team());
create policy ag_github_repos on public.github_repos for all to comrade_agent
  using (team_id = public.current_team()) with check (team_id = public.current_team());
create policy ag_github_activity on public.github_activity for all to comrade_agent
  using (team_id = public.current_team()) with check (team_id = public.current_team());
create policy ag_consent_queue on public.consent_queue for all to comrade_agent
  using (team_id = public.current_team()) with check (team_id = public.current_team());
create policy ag_agent_runs on public.agent_runs for all to comrade_agent
  using (team_id = public.current_team()) with check (team_id = public.current_team());
create policy ag_change_log on public.change_log for all to comrade_agent
  using (team_id = public.current_team()) with check (team_id = public.current_team());

-- ---- comrade_pipeline: document parser + memory COMPILER (sole memory writer) ----
grant select on
  public.teams, public.memberships, public.profiles, public.messages,
  public.documents, public.document_opens, public.github_activity,
  public.memory_compilations, public.memory_entries, public.memory_versions,
  public.memory_citations, public.memory_reverts, public.jobs, public.change_log
to comrade_pipeline;
grant insert, update on
  public.documents, public.jobs, public.messages, public.change_log,
  public.memory_compilations, public.memory_entries, public.memory_versions,
  public.memory_citations
to comrade_pipeline;

create policy pl_teams on public.teams for all to comrade_pipeline
  using (id = public.current_team()) with check (id = public.current_team());
create policy pl_memberships on public.memberships for all to comrade_pipeline
  using (team_id = public.current_team()) with check (team_id = public.current_team());
create policy pl_profiles on public.profiles for all to comrade_pipeline
  using (exists (select 1 from public.memberships mm
                 where mm.user_id = id and mm.team_id = public.current_team()))
  with check (exists (select 1 from public.memberships mm
                 where mm.user_id = id and mm.team_id = public.current_team()));
create policy pl_messages on public.messages for all to comrade_pipeline
  using (team_id = public.current_team()) with check (team_id = public.current_team());
create policy pl_documents on public.documents for all to comrade_pipeline
  using (team_id = public.current_team()) with check (team_id = public.current_team());
create policy pl_document_opens on public.document_opens for all to comrade_pipeline
  using (exists (select 1 from public.documents d
                 where d.id = document_id and d.team_id = public.current_team()))
  with check (exists (select 1 from public.documents d
                 where d.id = document_id and d.team_id = public.current_team()));
create policy pl_github_activity on public.github_activity for all to comrade_pipeline
  using (team_id = public.current_team()) with check (team_id = public.current_team());
create policy pl_memory_compilations on public.memory_compilations for all to comrade_pipeline
  using (team_id = public.current_team()) with check (team_id = public.current_team());
create policy pl_memory_entries on public.memory_entries for all to comrade_pipeline
  using (team_id = public.current_team()) with check (team_id = public.current_team());
create policy pl_memory_versions on public.memory_versions for all to comrade_pipeline
  using (team_id = public.current_team()) with check (team_id = public.current_team());
create policy pl_memory_citations on public.memory_citations for all to comrade_pipeline
  using (exists (select 1 from public.memory_versions v
                 where v.id = version_id and v.team_id = public.current_team()))
  with check (exists (select 1 from public.memory_versions v
                 where v.id = version_id and v.team_id = public.current_team()));
create policy pl_memory_reverts on public.memory_reverts for all to comrade_pipeline
  using (team_id = public.current_team()) with check (team_id = public.current_team());
create policy pl_jobs on public.jobs for all to comrade_pipeline
  using (team_id = public.current_team()) with check (team_id = public.current_team());
create policy pl_change_log on public.change_log for all to comrade_pipeline
  using (team_id = public.current_team()) with check (team_id = public.current_team());

-- ============================================================
-- 6. Contribution view runs with the QUERYING user's privileges,
--    so underlying table RLS applies (private messages auto-excluded).
-- ============================================================
alter view public.contribution_v set (security_invoker = on);
