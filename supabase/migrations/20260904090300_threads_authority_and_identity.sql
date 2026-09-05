-- Canonical thread membership controls both participant management and the
-- legacy bridge. Legacy columns must never recategorise restricted content.

drop policy au_thread_participants_insert on public.thread_participants;
drop policy au_thread_participants_delete on public.thread_participants;
create policy au_thread_participants_insert on public.thread_participants for insert to authenticated
  with check (
    added_by = (select auth.uid())
    and public.is_team_member(team_id)
    and public.is_thread_creator(thread_id, (select auth.uid()))
  );
create policy au_thread_participants_delete on public.thread_participants for delete to authenticated
  using (
    public.is_team_member(team_id)
    and (
      (user_id = (select auth.uid()) and not public.is_thread_creator(thread_id, (select auth.uid())))
      or (user_id <> (select auth.uid()) and public.is_thread_creator(thread_id, (select auth.uid())))
    )
  );

create or replace function public.trg_messages_legacy_thread()
returns trigger language plpgsql security definer set search_path = '' as $$
declare
  _visibility text;
begin
  if new.thread_id is null then
    if new.thread_type = 'group' and new.thread_owner_id is null then
      select id into new.thread_id from public.threads
      where team_id = new.team_id and visibility = 'team' and kind = 'discussion'
        and title = 'General';
    elsif new.thread_type = 'private' and new.thread_owner_id is not null then
      select id into new.thread_id from public.threads
      where team_id = new.team_id and legacy_thread_owner_id = new.thread_owner_id;
      if new.thread_id is null then
        insert into public.threads (
          team_id, title, visibility, kind, owner_id, created_by,
          legacy_thread_owner_id
        ) values (
          new.team_id, 'Private', 'restricted', 'discussion', new.thread_owner_id,
          new.thread_owner_id, new.thread_owner_id
        ) returning id into new.thread_id;
        insert into public.thread_participants (thread_id, team_id, user_id, added_by)
        values (new.thread_id, new.team_id, new.thread_owner_id, new.thread_owner_id);
      end if;
    else
      raise exception 'legacy message needs an unambiguous thread';
    end if;
  end if;

  select visibility into _visibility from public.threads
  where id = new.thread_id and team_id = new.team_id;
  if _visibility is null then
    raise exception 'message has no canonical thread';
  end if;
  if (new.thread_type = 'group') <> (_visibility = 'team') then
    raise exception 'legacy message type disagrees with canonical thread';
  end if;
  return new;
end;
$$;

create or replace function public.trg_thread_identity_guard()
returns trigger language plpgsql set search_path = '' as $$
begin
  if new.team_id is distinct from old.team_id
     or new.thread_id is distinct from old.thread_id
     or new.thread_type is distinct from old.thread_type
     or new.thread_owner_id is distinct from old.thread_owner_id
     or new.sender_id is distinct from old.sender_id
     or new.sender_kind is distinct from old.sender_kind then
    raise exception 'message identity cannot change';
  end if;
  return new;
end;
$$;
