-- Consent tiers (T0–T3 by blast radius) + the T3 second key + observation
-- suppressions.
--
-- Governance ruling (provisional, logged 2026-07): tiers grade by blast
-- radius, not rank. T0 read-only runs instantly; T1 affects one member (that
-- member consents — the assignee-confirm trigger generalized); T2 shared but
-- reversible (act + visible card + one-tap revert); T3 external/irreversible/
-- money needs TWO keys — the initiator plus any OTHER member. Money and
-- outbound-to-non-members never drop below T3.

alter table public.consent_queue
  add column tier text not null default 'T2'
    check (tier in ('T0','T1','T2','T3')),
  add column second_approver_id uuid references public.profiles(id) on delete set null,
  add column second_approved_at timestamptz,
  add constraint consent_second_key_distinct
    check (second_approver_id is null
           or second_approver_id <> requesting_member_id);

-- The schema comment predated the code; shared/consent.py's 7-day backstop is
-- authoritative.
comment on column public.consent_queue.expires_at is
  'TTL backstop — 7 days, stamped by shared/consent.py at propose time';

-- ---- visibility: teammates must SEE pending T3 items to countersign ----
-- (everything below T3 stays requester-only, per the original policy)
create policy au_consent_queue_select_t3 on public.consent_queue
  for select to authenticated
  using (tier = 'T3' and public.is_team_member(team_id));

-- ---- countersign: a teammate (never the requester) may add the second key ----
create policy au_consent_queue_second_key on public.consent_queue
  for update to authenticated
  using (tier = 'T3' and public.is_team_member(team_id)
         and requesting_member_id <> (select auth.uid()))
  with check (tier = 'T3' and public.is_team_member(team_id)
              and requesting_member_id <> (select auth.uid()));

-- The policy above opens UPDATE on the whole row; this trigger narrows what
-- each human actor may actually change. Worker roles (auth.uid() is null,
-- e.g. the executor flipping status) pass through untouched.
-- security definer: worker roles (executor/agent) have no USAGE on schema
-- auth, but the trigger must still ask auth.uid() who the actor is. Same
-- pattern as trg_memory_citation_source_team.
create or replace function public.trg_consent_second_key_guard()
returns trigger language plpgsql security definer set search_path = '' as $$
declare
  actor uuid := (select auth.uid());
begin
  if actor is null then
    return new;
  end if;

  if actor = old.requesting_member_id then
    -- The requester may never stamp their own second key…
    if (new.second_approver_id, new.second_approved_at) is distinct from
       (old.second_approver_id, old.second_approved_at) then
      raise exception 'requester cannot set the second key';
    end if;
    -- …and editing the action voids any countersign already given: the
    -- second key blessed what the teammate SAW, not whatever it became.
    if new.tool_args is distinct from old.tool_args
       or new.action_hash is distinct from old.action_hash then
      new.second_approver_id := null;
      new.second_approved_at := null;
    end if;
    return new;
  end if;

  -- A teammate's write is a countersign and nothing else.
  if old.status not in ('pending', 'approved') then
    raise exception 'consent item is no longer countersignable';
  end if;
  if new.second_approver_id is distinct from actor then
    raise exception 'second key must be stamped as yourself';
  end if;
  if (new.tier, new.team_id, new.requesting_member_id, new.tool_name,
      new.tool_args, new.source_snippet, new.action_hash, new.status,
      new.reversible, new.expires_at, new.resolved_at)
     is distinct from
     (old.tier, old.team_id, old.requesting_member_id, old.tool_name,
      old.tool_args, old.source_snippet, old.action_hash, old.status,
      old.reversible, old.expires_at, old.resolved_at) then
    raise exception 'countersigning may only set the second key';
  end if;
  new.second_approved_at := now();
  return new;
end;
$$;

create trigger trg_consent_second_key
  before update on public.consent_queue
  for each row execute function public.trg_consent_second_key_guard();

-- ---- observation suppressions: "remove + don't do this again" ----
-- Any member can suppress a category of proactive AI observation for the
-- team. The agent consults this before posting (enforcement lands with the
-- proactive/event-bus slice); the row itself is the standing request.
create table public.observation_suppressions (
  id         uuid primary key default gen_random_uuid(),
  team_id    uuid not null references public.teams(id) on delete cascade,
  member_id  uuid not null references public.profiles(id) on delete cascade,
  kind       text not null,          -- observation category being suppressed
  message_id uuid references public.messages(id) on delete set null,
  created_at timestamptz not null default now()
);
create index idx_obs_suppressions_team on public.observation_suppressions(team_id);

alter table public.observation_suppressions enable row level security;

-- members: see the team's suppressions, add their own, withdraw their own
create policy au_obs_suppressions_select on public.observation_suppressions
  for select to authenticated using (public.is_team_member(team_id));
create policy au_obs_suppressions_insert on public.observation_suppressions
  for insert to authenticated
  with check (public.is_team_member(team_id)
              and member_id = (select auth.uid()));
create policy au_obs_suppressions_delete on public.observation_suppressions
  for delete to authenticated using (member_id = (select auth.uid()));

-- agent: read-only (it must respect suppressions, never manage them)
grant select on public.observation_suppressions to comrade_agent;
create policy ag_obs_suppressions on public.observation_suppressions
  for select to comrade_agent using (team_id = public.current_team());
