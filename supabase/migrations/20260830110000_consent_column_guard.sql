-- A requester may change only what a decision needs, not the whole row.
--
-- Carried Important finding from Phase 0's whole-branch review.
-- au_consent_queue_update restricts which ROWS a member may update and nothing
-- else — RLS policies gate rows, not columns. So the requester could rewrite
-- any column on their own pending row:
--
--   * rewind status to 'pending' and re-approve  -> the action executes TWICE
--   * rewrite action_hash                        -> unbinds the approved action
--   * rewrite team_id / tier / expires_at        -> moves or re-grades the item
--
-- execute_consent's compare-and-swap makes execution exactly-once against
-- retries, races and the agent. It never defended against the requester,
-- because the CAS only refuses a row that is not currently approved/edited —
-- and rewinding the status makes it approvable again.
--
-- This is the last hole in the consent invariant. The mechanism is the one
-- deleted with T3 in Phase 0 (trg_consent_second_key_guard,
-- 20260719130000_consent_tiers.sql): a security definer trigger that narrows
-- what each human actor may actually change, letting worker roles through.
--
-- security definer: worker roles (executor/agent) have no USAGE on schema
-- auth, but the trigger must still ask auth.uid() who the actor is. Same
-- pattern as trg_memory_citation_source_team.

create or replace function public.trg_consent_requester_guard()
returns trigger language plpgsql security definer set search_path = '' as $$
declare
  actor uuid := (select auth.uid());
begin
  -- Worker roles pass through untouched: the executor flipping status to
  -- 'executed' is the mechanism this trigger exists to protect, not a threat.
  if actor is null then
    return new;
  end if;

  -- Columns no human actor may ever rewrite. Identity, grading and the
  -- lifetime of the request are set at propose time by a server-bound path.
  if (new.id, new.team_id, new.requesting_member_id, new.tool_name,
      new.source_snippet, new.reversible, new.tier, new.expires_at,
      new.created_at, new.agent_run_id, new.batch_id)
     is distinct from
     (old.id, old.team_id, old.requesting_member_id, old.tool_name,
      old.source_snippet, old.reversible, old.tier, old.expires_at,
      old.created_at, old.agent_run_id, old.batch_id) then
    raise exception 'a consent item''s identity and grading are not editable';
  end if;

  -- A decision is made once. Once a row leaves 'pending' it never returns:
  -- that is what stops approve -> rewind -> approve from executing twice.
  if old.status <> 'pending' then
    raise exception
      'consent item is already resolved (%) and cannot be changed', old.status;
  end if;

  if new.status not in ('approved', 'rejected', 'edited') then
    raise exception 'a requester may only approve, reject or edit (got %)',
      new.status;
  end if;

  -- tool_args and action_hash move together, and only on the edit path.
  -- edit_and_approve is the one legitimate rewrite: the member changed what
  -- they are approving, so the hash must be re-stamped to match. Changing the
  -- args WITHOUT re-stamping (or vice versa) is exactly the drift the hash
  -- exists to catch.
  if (new.tool_args, new.action_hash) is distinct from
     (old.tool_args, old.action_hash) and new.status <> 'edited' then
    raise exception 'the action may only change on the edit path';
  end if;

  return new;
end $$;

revoke all on function public.trg_consent_requester_guard() from public, anon, authenticated;

create trigger trg_consent_requester_guard
  before update on public.consent_queue
  for each row execute function public.trg_consent_requester_guard();
