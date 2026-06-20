-- Comrade — action/consent mechanism: two-role split + nudge cooldown.
--
-- Planner/executor separation: comrade_agent (which reasons over untrusted
-- content) can only PROPOSE gated actions (insert consent_queue) and send
-- private AI nudges. A separate comrade_executor performs the approved writes
-- (tasks, group messages). The LLM has no path to the executor.

-- ============================================================
-- 1. Executor role
-- ============================================================
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'comrade_executor') then
    create role comrade_executor nologin nobypassrls;
  end if;
end $$;

grant usage on schema public to comrade_executor;
grant execute on function public.current_team() to comrade_executor;

-- ============================================================
-- 2. Tighten comrade_agent: it may PROPOSE, not perform gated writes
-- ============================================================
revoke insert, update on public.tasks      from comrade_agent;
revoke insert, update on public.milestones  from comrade_agent;
revoke update          on public.consent_queue from comrade_agent;  -- propose=insert only
revoke update          on public.messages   from comrade_agent;     -- agent never edits messages

-- agent messages: read group+private (team), but INSERT only private AI nudges
drop policy if exists ag_messages on public.messages;
create policy ag_messages_select on public.messages for select to comrade_agent
  using (team_id = public.current_team());
create policy ag_messages_insert on public.messages for insert to comrade_agent
  with check (team_id = public.current_team()
              and thread_type = 'private' and sender_kind = 'ai');

-- ============================================================
-- 3. Executor grants + team-scoped policies
-- ============================================================
grant select on
  public.consent_queue, public.tasks, public.memberships,
  public.profiles, public.teams, public.messages
to comrade_executor;
grant insert, update on public.tasks         to comrade_executor;
grant insert         on public.messages      to comrade_executor;
grant update         on public.consent_queue to comrade_executor;

create policy ex_consent_queue on public.consent_queue for all to comrade_executor
  using (team_id = public.current_team()) with check (team_id = public.current_team());
create policy ex_tasks on public.tasks for all to comrade_executor
  using (team_id = public.current_team()) with check (team_id = public.current_team());
create policy ex_messages_select on public.messages for select to comrade_executor
  using (team_id = public.current_team());
create policy ex_messages_insert on public.messages for insert to comrade_executor
  with check (team_id = public.current_team() and sender_kind = 'ai');
create policy ex_memberships_select on public.memberships for select to comrade_executor
  using (team_id = public.current_team());
create policy ex_teams_select on public.teams for select to comrade_executor
  using (id = public.current_team());
create policy ex_profiles_select on public.profiles for select to comrade_executor
  using (exists (select 1 from public.memberships mm
                 where mm.user_id = profiles.id and mm.team_id = public.current_team()));

-- ============================================================
-- 4. Nudge cooldown log — exempt nudges have no human gate, so dedupe here
-- ============================================================
create table public.nudge_log (
  id         uuid primary key default gen_random_uuid(),
  team_id    uuid not null references public.teams(id) on delete cascade,
  member_id  uuid not null references public.profiles(id) on delete cascade,
  nudge_type text not null
    check (nudge_type in ('pending_task','overdue_deadline','idle','unopened_doc')),
  subject    text,                          -- what it's about (task id, doc id, ...)
  created_at timestamptz not null default now()
);
create index idx_nudge_log_lookup
  on public.nudge_log(team_id, member_id, nudge_type, created_at);

alter table public.nudge_log enable row level security;

-- a member can see nudges sent to them
create policy au_nudge_log_select on public.nudge_log for select to authenticated
  using (member_id = (select auth.uid()));
-- the agent records + checks cooldown, team-scoped
create policy ag_nudge_log on public.nudge_log for all to comrade_agent
  using (team_id = public.current_team()) with check (team_id = public.current_team());

grant select, insert on public.nudge_log to comrade_agent;
