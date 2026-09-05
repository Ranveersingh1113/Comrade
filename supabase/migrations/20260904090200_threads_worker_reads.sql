-- Workers need canonical thread IDs to attach system messages without falling
-- back to the legacy trigger. Their access stays constrained by current_team.

grant select on public.threads to comrade_agent, comrade_pipeline;

create policy ag_threads_select on public.threads for select to comrade_agent
  using (team_id = public.current_team());
create policy pl_threads_select on public.threads for select to comrade_pipeline
  using (team_id = public.current_team());

create or replace function public.ensure_legacy_private_thread(
  _team_id uuid, _member_id uuid
)
returns uuid language plpgsql security definer set search_path = '' as $$
declare
  _thread_id uuid;
begin
  if not exists (
    select 1 from public.memberships
    where team_id = _team_id and user_id = _member_id and status = 'active'
  ) then
    raise exception 'private thread owner must be an active team member';
  end if;

  select id into _thread_id from public.threads
  where team_id = _team_id and legacy_thread_owner_id = _member_id;
  if _thread_id is null then
    insert into public.threads (
      team_id, title, visibility, kind, owner_id, created_by,
      legacy_thread_owner_id
    ) values (
      _team_id, 'Private', 'restricted', 'discussion', _member_id, _member_id,
      _member_id
    ) on conflict do nothing returning id into _thread_id;
    if _thread_id is null then
      select id into _thread_id from public.threads
      where team_id = _team_id and legacy_thread_owner_id = _member_id;
    else
      insert into public.thread_participants (thread_id, team_id, user_id, added_by)
      values (_thread_id, _team_id, _member_id, _member_id);
    end if;
  end if;
  return _thread_id;
end;
$$;
revoke all on function public.ensure_legacy_private_thread(uuid, uuid) from public, anon, authenticated;
grant execute on function public.ensure_legacy_private_thread(uuid, uuid) to comrade_agent;
