-- A new thread opens immediately. Its first Comrade-directed message gives it
-- a useful name in the same transaction that records and queues that message.
drop function public.enqueue_agent_turn(uuid, uuid, text, text);

create function public.enqueue_agent_turn(
  p_team_id uuid,
  p_thread_id uuid,
  p_text text,
  p_trigger_type text default 'user'
) returns table (run_id uuid, message_id uuid, disposition text)
language plpgsql security definer set search_path = '' as $$
declare
  v_user_id uuid := auth.uid();
  v_title text;
begin
  if v_user_id is null
    or not public.can_access_thread(p_thread_id, v_user_id)
    or not exists (
      select 1 from public.threads where id = p_thread_id and team_id = p_team_id
    ) then
    raise exception 'thread not accessible' using errcode = '42501';
  end if;

  v_title := left(
    regexp_replace(
      regexp_replace(btrim(p_text), '^@comrade[[:space:]]*', '', 'i'),
      '[[:space:]]+', ' ', 'g'
    ),
    80
  );
  if v_title <> '' then
    update public.threads
    set title = v_title, updated_at = now()
    where id = p_thread_id and team_id = p_team_id and title = 'New thread';
  end if;

  insert into public.messages (team_id, thread_id, sender_kind, sender_id, body)
  values (p_team_id, p_thread_id, 'user', v_user_id, p_text)
  returning id into message_id;

  select id into run_id
  from public.agent_runs
  where thread_id = p_thread_id
    and status in ('running', 'waiting_for_permission', 'waiting_for_user')
  order by created_at
  limit 1;
  if run_id is not null then
    disposition := 'steering';
    return next;
    return;
  end if;

  insert into public.agent_runs (
    team_id, requester_id, thread_id, input_message_id, trigger_type,
    input_summary, status
  ) values (
    p_team_id, v_user_id, p_thread_id, message_id, p_trigger_type,
    left(p_text, 200), 'queued'
  ) returning id into run_id;
  disposition := 'queued';
  return next;
end;
$$;

revoke all on function public.enqueue_agent_turn(uuid, uuid, text, text) from public, anon;
grant execute on function public.enqueue_agent_turn(uuid, uuid, text, text) to authenticated;
