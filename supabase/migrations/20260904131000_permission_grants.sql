-- A reusable approval is narrow: one requester, one visible thread, one
-- action and one normalized resource. Everything else remains Allow once.
create table public.permission_grants (
  id uuid primary key default gen_random_uuid(),
  team_id uuid not null references public.teams(id) on delete cascade,
  thread_id uuid not null,
  requesting_member_id uuid not null references public.profiles(id) on delete cascade,
  tool_name text not null,
  resource_constraint jsonb not null,
  max_risk_class text not null check (max_risk_class in ('member', 'shared')),
  source_consent_id uuid not null unique references public.consent_queue(id) on delete restrict,
  expires_at timestamptz not null,
  created_at timestamptz not null default now(),
  revoked_at timestamptz,
  foreign key (thread_id, team_id) references public.threads(id, team_id) on delete cascade
);

create unique index uq_active_permission_grant_scope
  on public.permission_grants (
    team_id, thread_id, requesting_member_id, tool_name, resource_constraint
  ) where revoked_at is null;

alter table public.permission_grants enable row level security;
revoke all on public.permission_grants from authenticated, comrade_agent;
grant select, insert, update on public.permission_grants to authenticated;
grant select on public.permission_grants to comrade_agent;

create policy au_permission_grants_select on public.permission_grants for select to authenticated
  using (
    requesting_member_id = (select auth.uid())
    and public.can_access_thread(thread_id, (select auth.uid()))
  );
create policy au_permission_grants_insert on public.permission_grants for insert to authenticated
  with check (
    requesting_member_id = (select auth.uid())
    and public.can_access_thread(thread_id, (select auth.uid()))
  );
create policy au_permission_grants_update on public.permission_grants for update to authenticated
  using (
    requesting_member_id = (select auth.uid())
    and public.can_access_thread(thread_id, (select auth.uid()))
  ) with check (
    requesting_member_id = (select auth.uid())
    and public.can_access_thread(thread_id, (select auth.uid()))
  );
create policy ag_permission_grants_select on public.permission_grants for select to comrade_agent
  using (team_id = public.current_team());

-- Table permissions prove who may write. This trigger proves what they may
-- write: grants are minted only from an executed, grantable consent action.
create or replace function public.trg_permission_grant_guard()
returns trigger language plpgsql security definer set search_path = '' as $$
declare
  actor uuid := (select auth.uid());
  source record;
  expected_resource jsonb;
begin
  if tg_op = 'UPDATE' then
    if actor is null
       or old.requesting_member_id <> actor
       or old.revoked_at is not null
       or new.revoked_at is null
       or (new.id, new.team_id, new.thread_id, new.requesting_member_id,
           new.tool_name, new.resource_constraint, new.max_risk_class,
           new.source_consent_id, new.expires_at, new.created_at)
          is distinct from
          (old.id, old.team_id, old.thread_id, old.requesting_member_id,
           old.tool_name, old.resource_constraint, old.max_risk_class,
           old.source_consent_id, old.expires_at, old.created_at) then
      raise exception 'a permission grant may only be revoked by its requester';
    end if;
    return new;
  end if;

  if actor is null or new.requesting_member_id <> actor then
    raise exception 'a permission grant must be created by its requester';
  end if;
  select team_id, thread_id, requesting_member_id, tool_name, tool_args, status
    into source
    from public.consent_queue where id = new.source_consent_id;
  if not found or source.status <> 'executed'
     or source.team_id <> new.team_id
     or source.thread_id is distinct from new.thread_id
     or source.requesting_member_id <> new.requesting_member_id
     or source.tool_name <> new.tool_name then
    raise exception 'permission grant must match an executed thread consent';
  end if;

  case new.tool_name
    when 'task_create' then
      expected_resource := jsonb_build_object('assignee_id', source.tool_args -> 'assignee_id');
    when 'task_update' then
      expected_resource := jsonb_build_object('task_id', source.tool_args -> 'task_id');
    else
      raise exception 'this action may only be allowed once';
  end case;
  if new.resource_constraint is distinct from expected_resource
     or new.max_risk_class <> 'member'
     or new.expires_at <= now()
     or new.expires_at > now() + interval '24 hours' then
    raise exception 'invalid permission grant scope';
  end if;
  return new;
end;
$$;

revoke all on function public.trg_permission_grant_guard() from public, anon, authenticated;
create trigger trg_permission_grant_guard
  before insert or update on public.permission_grants
  for each row execute function public.trg_permission_grant_guard();

-- Participants can inspect an inline action card; only the requester retains
-- update access through the older requester-only update policy.
drop policy au_consent_queue_select on public.consent_queue;
create policy au_consent_queue_select on public.consent_queue for select to authenticated
  using (
    requesting_member_id = (select auth.uid())
    or (thread_id is not null and public.can_access_thread(thread_id, (select auth.uid())))
  );
