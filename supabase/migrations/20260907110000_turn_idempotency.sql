-- A retried send must not post the message twice.
--
-- 🔴 A turn arrived with nothing identifying the ATTEMPT, so an accepted POST
-- whose connection then died was indistinguishable from one that never
-- arrived. The browser put the text back in the composer, the member pressed
-- send again, and the thread got the same question twice — two messages, two
-- runs, two model bills.
--
-- Expand-then-contract: the column is nullable and the index is partial, so
-- every existing row and every client that does not send an id keeps working.
alter table public.messages
  add column if not exists client_request_id text;

-- The uniqueness rule, and the reason the lookup below is safe. A lookup on
-- its own is check-then-act: two simultaneous retries both miss, both insert.
-- This index is what actually holds the line; the lookup only avoids raising.
create unique index if not exists messages_client_request_id_key
  on public.messages (team_id, sender_id, thread_id, client_request_id)
  where client_request_id is not null;

-- Added ALONGSIDE the four-argument form, not in place of it. Releases build,
-- then migrate, then activate (scripts/deploy_host.sh), so between the
-- migration and the new image there is a window where the RUNNING api still
-- calls the old signature. Dropping it here would make that window an outage.
-- The old form becomes a shim below; a later release contracts it away.
--
-- The fifth parameter takes NO default on purpose: with one, a four-argument
-- call would match both signatures and Postgres would refuse it as ambiguous.
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

  -- Already accepted? Return what it became. A duplicate is not an error: the
  -- member's question is in the thread and is being answered, and telling them
  -- otherwise is how the second copy gets sent.
  if p_client_request_id is not null then
    select id into message_id
    from public.messages
    where team_id = p_team_id and thread_id = p_thread_id
      and sender_id = v_user_id and client_request_id = p_client_request_id;
    if message_id is not null then
      select id into run_id from public.agent_runs
      where input_message_id = message_id;
      if run_id is null then
        -- A steering message attaches to whatever run was active rather than
        -- owning one. Follow that run, or the newest, so the browser has
        -- something to reconnect to instead of a null.
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
    team_id, thread_id, sender_kind, sender_id, body, client_request_id
  )
  values (p_team_id, p_thread_id, 'user', v_user_id, p_text, p_client_request_id)
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

-- The compatibility shim. Anything still on the old signature keeps working
-- and simply sends no request id, which is exactly the pre-migration
-- behaviour: no deduplication, no failure.
create or replace function public.enqueue_agent_turn(
  p_team_id uuid,
  p_thread_id uuid,
  p_text text,
  p_trigger_type text default 'user'
) returns table (run_id uuid, message_id uuid, disposition text)
language sql set search_path = '' as $$
  select * from public.enqueue_agent_turn(
    p_team_id, p_thread_id, p_text, p_trigger_type, null::text
  );
$$;

revoke all on function public.enqueue_agent_turn(uuid, uuid, text, text, text) from public, anon;
grant execute on function public.enqueue_agent_turn(uuid, uuid, text, text, text) to authenticated;
revoke all on function public.enqueue_agent_turn(uuid, uuid, text, text) from public, anon;
grant execute on function public.enqueue_agent_turn(uuid, uuid, text, text) to authenticated;
