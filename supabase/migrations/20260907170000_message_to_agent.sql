-- Whether a message was addressed to Comrade, recorded on the message.
--
-- 🔴 Nothing recorded it, so "@comrade investigate switching to Postgres" and
-- "we are switching to Postgres" reached the memory compiler as the same kind
-- of sentence — and a request to look INTO an option could be compiled into
-- the wiki as a decision the team had TAKEN. The wiki is what the agent reads
-- back as fact on every later turn, so that error compounds.
--
-- On the message rather than derived from agent_runs at read time: that table
-- deliberately has no broad grant (findings §4.1 — it holds private prompts
-- and tool results), and widening it so the compiler can ask one boolean
-- question would trade a real privacy boundary for a convenience. The message
-- records what it was.
alter table public.messages
  add column if not exists to_agent boolean not null default false;

comment on column public.messages.to_agent is
  'True when this message was sent as a turn to Comrade rather than to the '
  'team. Context for memory extraction: a request to investigate is not a '
  'decision. It does not disqualify one either — a decision announced to '
  'Comrade is still a decision.';

-- The enqueue path is the only place a member-authored agent turn is written,
-- so it is the only place that needs to say so.
create or replace function public.enqueue_agent_turn(
  p_team_id uuid,
  p_thread_id uuid,
  p_text text,
  p_trigger_type text,
  p_client_request_id text
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

  if p_client_request_id is not null then
    select id into message_id
    from public.messages
    where team_id = p_team_id and thread_id = p_thread_id
      and sender_id = v_user_id and client_request_id = p_client_request_id;
    if message_id is not null then
      select id into run_id from public.agent_runs
      where input_message_id = message_id;
      if run_id is null then
        select id into run_id from public.agent_runs
        where thread_id = p_thread_id
        order by (status in ('running', 'waiting_for_permission', 'waiting_for_user')) desc,
                 created_at desc
        limit 1;
      end if;
      disposition := 'duplicate';
      return next;
      return;
    end if;
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

  insert into public.messages (
    team_id, thread_id, sender_kind, sender_id, body, client_request_id,
    to_agent
  )
  values (p_team_id, p_thread_id, 'user', v_user_id, p_text,
          p_client_request_id, true)
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
