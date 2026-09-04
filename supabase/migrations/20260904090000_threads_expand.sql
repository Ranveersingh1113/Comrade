-- Canonical conversation scope. Legacy message columns stay during transition.

create table public.threads (
  id                     uuid primary key default gen_random_uuid(),
  team_id                uuid not null references public.teams(id) on delete cascade,
  title                  text not null check (length(trim(title)) > 0),
  visibility             text not null default 'team' check (visibility in ('team', 'restricted')),
  kind                   text not null default 'discussion' check (kind in ('discussion', 'work')),
  work_state             text check (work_state in ('planned', 'active', 'waiting', 'review', 'done')),
  owner_id               uuid references public.profiles(id) on delete set null,
  due_at                 timestamptz,
  created_by             uuid references public.profiles(id) on delete set null,
  legacy_thread_owner_id uuid references public.profiles(id) on delete restrict,
  created_at             timestamptz not null default now(),
  updated_at             timestamptz not null default now(),
  archived_at            timestamptz,
  unique (id, team_id),
  check ((kind = 'work') = (work_state is not null))
);

create unique index uq_threads_general on public.threads (team_id)
  where visibility = 'team' and kind = 'discussion' and title = 'General';
create unique index uq_threads_legacy_private on public.threads (team_id, legacy_thread_owner_id)
  where legacy_thread_owner_id is not null;
create index idx_threads_team_updated on public.threads (team_id, updated_at desc);

create table public.thread_participants (
  thread_id uuid not null,
  team_id   uuid not null,
  user_id   uuid not null references public.profiles(id) on delete cascade,
  added_by  uuid not null references public.profiles(id) on delete restrict,
  joined_at timestamptz not null default now(),
  primary key (thread_id, user_id),
  foreign key (thread_id, team_id) references public.threads (id, team_id) on delete cascade
);
create index idx_thread_participants_user on public.thread_participants (user_id, thread_id);

create or replace function public.can_access_thread(_thread_id uuid, _user_id uuid)
returns boolean language sql stable security definer set search_path = '' as $$
  select exists (
    select 1
    from public.threads t
    join public.memberships m on m.team_id = t.team_id
    where t.id = _thread_id and m.user_id = _user_id and m.status = 'active'
      and (
        t.visibility = 'team'
        or exists (
          select 1 from public.thread_participants p
          where p.thread_id = t.id and p.user_id = _user_id
        )
      )
  );
$$;
revoke all on function public.can_access_thread(uuid, uuid) from public, anon;
grant execute on function public.can_access_thread(uuid, uuid)
  to authenticated, comrade_agent, comrade_pipeline;

create or replace function public.is_thread_creator(_thread_id uuid, _user_id uuid)
returns boolean language sql stable security definer set search_path = '' as $$
  select exists (
    select 1 from public.threads
    where id = _thread_id and created_by = _user_id
  );
$$;
revoke all on function public.is_thread_creator(uuid, uuid) from public, anon;
grant execute on function public.is_thread_creator(uuid, uuid) to authenticated;

alter table public.messages add column thread_id uuid;
alter table public.agent_runs
  add column thread_id uuid,
  add column requester_id uuid references public.profiles(id) on delete set null,
  add column input_message_id uuid references public.messages(id) on delete set null;

-- One public thread per existing team, then one legacy-restricted thread per
-- old private owner. The compatibility field is only for this bridge.
insert into public.threads (team_id, title, visibility, kind, created_by)
select t.id, 'General', 'team', 'discussion', coalesce(t.created_by, m.user_id)
from public.teams t
left join lateral (
  select user_id from public.memberships
  where team_id = t.id and status = 'active'
  order by created_at, id limit 1
) m on true;

insert into public.threads (
  team_id, title, visibility, kind, created_by, legacy_thread_owner_id
)
select distinct m.team_id, 'Private', 'restricted', 'discussion',
  m.thread_owner_id, m.thread_owner_id
from public.messages m
where m.thread_type = 'private' and m.thread_owner_id is not null;

insert into public.thread_participants (thread_id, team_id, user_id, added_by)
select t.id, t.team_id, t.legacy_thread_owner_id, t.legacy_thread_owner_id
from public.threads t
where t.legacy_thread_owner_id is not null;

create or replace function public.trg_team_general_thread()
returns trigger language plpgsql security definer set search_path = '' as $$
begin
  insert into public.threads (team_id, title, visibility, kind, created_by)
  values (new.id, 'General', 'team', 'discussion', new.created_by);
  return new;
end;
$$;
drop trigger if exists trg_team_general_thread on public.teams;
create trigger trg_team_general_thread
  after insert on public.teams
  for each row execute function public.trg_team_general_thread();

update public.messages m set thread_id = t.id
from public.threads t
where m.thread_type = 'group' and t.team_id = m.team_id
  and t.visibility = 'team' and t.kind = 'discussion' and t.title = 'General';

update public.messages m set thread_id = t.id
from public.threads t
where m.thread_type = 'private' and t.team_id = m.team_id
  and t.legacy_thread_owner_id = m.thread_owner_id;

alter table public.messages
  add constraint messages_thread_team_fk foreign key (thread_id, team_id)
  references public.threads (id, team_id) on delete restrict;
alter table public.agent_runs
  add constraint agent_runs_thread_team_fk foreign key (thread_id, team_id)
  references public.threads (id, team_id) on delete restrict;
alter table public.messages alter column thread_id set not null;

create or replace function public.trg_thread_participant_membership()
returns trigger language plpgsql security definer set search_path = '' as $$
begin
  if not exists (
    select 1 from public.memberships
    where team_id = new.team_id and user_id = new.user_id and status = 'active'
  ) then
    raise exception 'thread participant must be an active team member';
  end if;
  return new;
end;
$$;
create trigger trg_thread_participant_membership
  before insert or update of team_id, user_id on public.thread_participants
  for each row execute function public.trg_thread_participant_membership();

create or replace function public.trg_messages_legacy_thread()
returns trigger language plpgsql security definer set search_path = '' as $$
begin
  if new.thread_id is not null then
    return new;
  end if;
  if new.thread_type = 'group' and new.thread_owner_id is null then
    select id into new.thread_id from public.threads
    where team_id = new.team_id and visibility = 'team' and kind = 'discussion'
      and title = 'General';
  elsif new.thread_type = 'private' and new.thread_owner_id is not null then
    select id into new.thread_id from public.threads
    where team_id = new.team_id and legacy_thread_owner_id = new.thread_owner_id;
    if new.thread_id is null then
      insert into public.threads (
        team_id, title, visibility, kind, created_by, legacy_thread_owner_id
      ) values (
        new.team_id, 'Private', 'restricted', 'discussion', new.thread_owner_id,
        new.thread_owner_id
      ) returning id into new.thread_id;
      insert into public.thread_participants (thread_id, team_id, user_id, added_by)
      values (new.thread_id, new.team_id, new.thread_owner_id, new.thread_owner_id);
    end if;
  else
    raise exception 'legacy message needs an unambiguous thread';
  end if;
  if new.thread_id is null then
    raise exception 'legacy message has no canonical thread';
  end if;
  return new;
end;
$$;

drop trigger if exists trg_messages_legacy_thread on public.messages;
create trigger trg_messages_legacy_thread
  before insert on public.messages
  for each row execute function public.trg_messages_legacy_thread();

create or replace function public.trg_thread_identity_guard()
returns trigger language plpgsql set search_path = '' as $$
begin
  if new.team_id is distinct from old.team_id
     or new.thread_id is distinct from old.thread_id
     or new.sender_id is distinct from old.sender_id
     or new.sender_kind is distinct from old.sender_kind then
    raise exception 'message identity cannot change';
  end if;
  return new;
end;
$$;
drop trigger if exists trg_messages_thread_identity_guard on public.messages;
create trigger trg_messages_thread_identity_guard
  before update on public.messages
  for each row execute function public.trg_thread_identity_guard();

alter table public.threads enable row level security;
alter table public.thread_participants enable row level security;

revoke all on public.threads, public.thread_participants from authenticated;
grant select, insert on public.threads to authenticated;
grant select, insert, delete on public.thread_participants to authenticated;

create policy au_threads_select on public.threads for select to authenticated
  using (public.can_access_thread(id, (select auth.uid())));
create policy au_threads_insert on public.threads for insert to authenticated
  with check (
    created_by = (select auth.uid()) and public.is_team_member(team_id)
  );
create policy au_thread_participants_select on public.thread_participants for select to authenticated
  using (public.can_access_thread(thread_id, (select auth.uid())));
create policy au_thread_participants_insert on public.thread_participants for insert to authenticated
  with check (
    added_by = (select auth.uid())
    and public.is_thread_creator(thread_id, (select auth.uid()))
  );
create policy au_thread_participants_delete on public.thread_participants for delete to authenticated
  using (
    (user_id = (select auth.uid()) and not public.is_thread_creator(thread_id, (select auth.uid())))
    or (user_id <> (select auth.uid()) and public.is_thread_creator(thread_id, (select auth.uid())))
  );

drop policy if exists au_messages_select on public.messages;
drop policy if exists au_messages_insert on public.messages;
drop policy if exists au_messages_update on public.messages;
create policy au_messages_select on public.messages for select to authenticated
  using (thread_id is not null and public.can_access_thread(thread_id, (select auth.uid())));
create policy au_messages_insert on public.messages for insert to authenticated
  with check (
    sender_kind = 'user' and sender_id = (select auth.uid())
    and thread_id is not null and public.can_access_thread(thread_id, (select auth.uid()))
  );
create policy au_messages_update on public.messages for update to authenticated
  using (sender_id = (select auth.uid()) and public.can_access_thread(thread_id, (select auth.uid())))
  with check (sender_id = (select auth.uid()) and public.can_access_thread(thread_id, (select auth.uid())));
