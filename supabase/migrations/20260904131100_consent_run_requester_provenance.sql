-- ToolContext binds requester and run together. Preserve that binding in the
-- database too, so no caller can attach another member's identity to a run.
create or replace function public.trg_consent_provenance_guard()
returns trigger language plpgsql security definer set search_path = '' as $$
declare
  run_team uuid;
  run_thread uuid;
  run_requester uuid;
begin
  if tg_op = 'UPDATE'
     and (new.thread_id, new.agent_run_id) is distinct from (old.thread_id, old.agent_run_id) then
    raise exception 'consent provenance cannot change';
  end if;
  if new.agent_run_id is not null then
    if new.thread_id is null then
      raise exception 'agent consent requires a thread';
    end if;
    select team_id, thread_id, requester_id
      into run_team, run_thread, run_requester
      from public.agent_runs where id = new.agent_run_id;
    if run_team is distinct from new.team_id
       or run_thread is distinct from new.thread_id
       or run_requester is distinct from new.requesting_member_id then
      raise exception 'consent provenance does not match its agent run';
    end if;
  end if;
  return new;
end;
$$;
