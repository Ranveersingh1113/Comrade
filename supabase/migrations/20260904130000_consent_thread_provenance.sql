-- Inline permissions belong to the conversation and run that requested them.
-- Older, non-agent lifecycle requests remain nullable until their callers move
-- to system threads in the remainder of Task 11.
alter table public.consent_queue add column thread_id uuid;
alter table public.consent_queue
  add constraint consent_queue_thread_team_fk foreign key (thread_id, team_id)
  references public.threads (id, team_id);

create or replace function public.trg_consent_provenance_guard()
returns trigger language plpgsql security definer set search_path = '' as $$
declare
  run_team uuid;
  run_thread uuid;
begin
  if tg_op = 'UPDATE'
     and (new.thread_id, new.agent_run_id) is distinct from (old.thread_id, old.agent_run_id) then
    raise exception 'consent provenance cannot change';
  end if;
  if new.agent_run_id is not null then
    if new.thread_id is null then
      raise exception 'agent consent requires a thread';
    end if;
    select team_id, thread_id into run_team, run_thread
    from public.agent_runs where id = new.agent_run_id;
    if run_team is distinct from new.team_id or run_thread is distinct from new.thread_id then
      raise exception 'consent provenance does not match its agent run';
    end if;
  end if;
  return new;
end;
$$;

create trigger trg_consent_provenance_guard
  before insert or update on public.consent_queue
  for each row execute function public.trg_consent_provenance_guard();
