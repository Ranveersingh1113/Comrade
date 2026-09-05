-- Consent cards now live in their originating thread. Migrate every still-live
-- legacy card before deleting the detached inbox's batch column.
alter table public.consent_queue disable trigger trg_consent_provenance_guard;

update public.consent_queue q
set thread_id = public.ensure_private_thread(q.team_id, q.requesting_member_id)
from public.memberships m
where q.thread_id is null
  and q.requesting_member_id is not null
  and m.team_id = q.team_id
  and m.user_id = q.requesting_member_id
  and m.status = 'active'
  and (q.expires_at is null or q.expires_at > now());

alter table public.consent_queue enable trigger trg_consent_provenance_guard;

do $$
begin
  if exists (
    select 1 from public.consent_queue
    where thread_id is null and (expires_at is null or expires_at > now())
  ) then
    raise exception 'cannot remove consent inbox while live orphan cards exist';
  end if;
  if exists (
    select 1 from public.consent_queue
    where batch_id is not null and status = 'pending'
      and (expires_at is null or expires_at > now())
  ) then
    raise exception 'cannot remove consent batch_id while live batch cards exist';
  end if;
end;
$$;

alter table public.consent_queue drop column batch_id;

create or replace function public.trg_consent_requester_guard()
returns trigger language plpgsql security definer set search_path = '' as $$
declare
  actor uuid := (select auth.uid());
begin
  if actor is null then
    return new;
  end if;

  if (new.id, new.team_id, new.thread_id, new.requesting_member_id, new.tool_name,
      new.source_snippet, new.reversible, new.tier, new.expires_at,
      new.created_at, new.agent_run_id)
     is distinct from
     (old.id, old.team_id, old.thread_id, old.requesting_member_id, old.tool_name,
      old.source_snippet, old.reversible, old.tier, old.expires_at,
      old.created_at, old.agent_run_id) then
    raise exception 'a consent item''s identity and grading are not editable';
  end if;

  if old.status <> 'pending' then
    raise exception
      'consent item is already resolved (%) and cannot be changed', old.status;
  end if;
  if new.status not in ('approved', 'rejected', 'edited') then
    raise exception 'a requester may only approve, reject or edit (got %)', new.status;
  end if;
  if (new.tool_args, new.action_hash) is distinct from
     (old.tool_args, old.action_hash) and new.status <> 'edited' then
    raise exception 'the action may only change on the edit path';
  end if;
  return new;
end $$;
