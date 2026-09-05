-- Canonical threads are now the sole message identity. Do not silently route
-- an incomplete write: every new message must name its thread explicitly.

do $$
begin
  if exists (select 1 from public.messages where thread_id is null) then
    raise exception 'cannot complete thread contract while orphan messages exist';
  end if;
end;
$$;

alter table public.messages alter column thread_id set not null;
drop trigger if exists trg_messages_legacy_thread on public.messages;
drop function if exists public.trg_messages_legacy_thread();

-- Existing personal threads keep their owner as canonical metadata before the
-- bridge field disappears. A nudge may create one personal discussion later.
update public.threads
set owner_id = legacy_thread_owner_id
where owner_id is null and legacy_thread_owner_id is not null;

drop function if exists public.ensure_legacy_private_thread(uuid, uuid);
drop index if exists public.uq_threads_legacy_private;
alter table public.threads drop column legacy_thread_owner_id;

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

create unique index uq_threads_personal_discussion
  on public.threads (team_id, owner_id)
  where visibility = 'restricted' and kind = 'discussion' and title = 'Private';

create function public.ensure_private_thread(_team_id uuid, _member_id uuid)
returns uuid language plpgsql security definer set search_path = '' as $$
declare
  _thread_id uuid;
begin
  select id into _thread_id from public.threads
  where team_id = _team_id and owner_id = _member_id
    and visibility = 'restricted' and kind = 'discussion' and title = 'Private';
  if _thread_id is null then
    insert into public.threads (team_id, title, visibility, kind, owner_id, created_by)
    values (_team_id, 'Private', 'restricted', 'discussion', _member_id, _member_id)
    on conflict do nothing returning id into _thread_id;
    if _thread_id is null then
      select id into _thread_id from public.threads
      where team_id = _team_id and owner_id = _member_id
        and visibility = 'restricted' and kind = 'discussion' and title = 'Private';
    else
      insert into public.thread_participants (thread_id, team_id, user_id, added_by)
      values (_thread_id, _team_id, _member_id, _member_id);
    end if;
  end if;
  return _thread_id;
end;
$$;
revoke all on function public.ensure_private_thread(uuid, uuid) from public, anon, authenticated;
grant execute on function public.ensure_private_thread(uuid, uuid) to comrade_agent;

create or replace view public.contribution_v as
select
  m.team_id,
  m.user_id,
  (select count(*) from public.tasks t
     where t.team_id = m.team_id and t.assignee_id = m.user_id
       and t.status = 'done') as tasks_done,
  (select count(*) from public.tasks t
     where t.team_id = m.team_id and t.assignee_id = m.user_id
       and t.status in ('confirmed','in_progress','done')) as tasks_active,
  (select count(*) from public.github_activity g
     where g.team_id = m.team_id and g.author_user_id = m.user_id) as github_events,
  (select count(*) from public.messages msg
     join public.threads th on th.id = msg.thread_id and th.team_id = msg.team_id
     where msg.team_id = m.team_id and th.visibility = 'team'
       and msg.sender_kind = 'user' and msg.sender_id = m.user_id
       and msg.deleted_scope is null) as group_messages,
  (select max(msg.created_at) from public.messages msg
     join public.threads th on th.id = msg.thread_id and th.team_id = msg.team_id
     where msg.team_id = m.team_id and th.visibility = 'team'
       and msg.sender_kind = 'user' and msg.sender_id = m.user_id
       and msg.deleted_scope is null) as last_message_at,
  (select max(t.updated_at) from public.tasks t
     where t.team_id = m.team_id and t.assignee_id = m.user_id) as last_task_at
from public.memberships m
where m.status = 'active';
alter view public.contribution_v set (security_invoker = on);

drop index if exists public.idx_messages_group_human;
drop index if exists public.idx_messages_private_owner;
drop index if exists public.idx_messages_team_thread;
alter table public.messages drop column thread_owner_id;
alter table public.messages drop column thread_type;

create index concurrently if not exists idx_messages_team_visible_human
  on public.messages (team_id, created_at)
  where sender_kind = 'user' and deleted_scope is null;
