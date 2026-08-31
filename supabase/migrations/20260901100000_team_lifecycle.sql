-- D4: a team is a thing you can leave, and nobody can push you out.
--
-- Two defects, one shape. `memberships.status` was ('invited','active') and
-- `au_memberships_delete` was `is_team_leader(team_id)`, so:
--
--   * nobody could leave a team they had joined -- the only exit was the
--     leader deleting your row; and
--   * the leader could eject any member at will.
--
-- The second is the one that matters for what this product is. §23.1: "nobody
-- approves another member's actions, nobody configures permissions." An admin
-- power to remove a peer is precisely the shape being argued against, and it
-- had been sitting in the schema since the first migration.
--
-- So: leaving is unilateral and self-service; being removed is not something
-- that happens TO you. Another member can only ASK, through the consent queue,
-- and the key that resolves it is your own (shared/consent.py, member_depart).

-- ============================================================
-- 1. 'left' is a state, not a deletion
-- ============================================================
-- The codebase's own rule -- deletion leaves a trace. A departed member's
-- messages, tasks and memory citations keep an author the roster can still
-- resolve (GroupRoom.tsx already renders 'Former member' for a missing
-- profile), and the row itself is the record that they were here and when
-- they went. A DELETE would erase the fact of the membership along with the
-- access it carried, which are different things.

alter table public.memberships
  drop constraint if exists memberships_status_check;
alter table public.memberships
  add constraint memberships_status_check
  check (status in ('invited','active','left'));

alter table public.memberships
  add column if not exists left_at timestamptz;

comment on column public.memberships.left_at is
  'when this member left; null while invited or active';

-- A team ends when its last member leaves. There is deliberately no
-- delete-team button: a team with no active members is ALREADY invisible to
-- everyone (is_team_member is false for all of them), so a delete would be a
-- second mechanism for a state the schema reaches on its own -- and an admin
-- power besides, since somebody would have to hold it. What was actually
-- missing is the trace: archived_at says the team ended and when, rather than
-- leaving a row that merely looks abandoned. §23.3 retains the history either
-- way.
alter table public.teams
  add column if not exists archived_at timestamptz;

comment on column public.teams.archived_at is
  'stamped when the last active member leaves; cleared if anyone rejoins';

-- ============================================================
-- 2. The transition guard
-- ============================================================
-- A policy decides WHICH ROWS you may touch. It cannot compare the old row
-- with the new one, so it cannot express "you may go active -> left but never
-- left -> active" -- and without that, adding 'left' would open a hole bigger
-- than the one it closes: au_memberships_update already lets you write your
-- OWN row, so a departed member could set themselves back to 'active' and
-- every policy in the schema would believe them again.
--
-- That is a state machine, and in this schema state machines live in BEFORE
-- triggers (trg_tasks_confirm_guard is the precedent). This extends the guard
-- 20260831120000 already put on this table rather than adding a second one.
--
--   invited -> active    the invitee, accepting
--   active  -> left      the member themselves, leaving
--   left    -> invited   an active leader, inviting them back
--
-- Everything else is refused. Note what is NOT here: no active -> left by
-- anyone but you. That absence is the §23.1 guarantee, stated once, in the
-- one place every writer passes through.

create or replace function public.trg_membership_identity_guard()
returns trigger language plpgsql security definer set search_path = '' as $$
declare
  actor uuid := (select auth.uid());
begin
  -- Applies to EVERY actor, worker roles included. A membership is the bond
  -- between one person and one team; rebinding it is never a legitimate edit,
  -- it is how you would teleport an account into someone else's team. This
  -- used to return early for workers, which was safe only while no worker
  -- could UPDATE this table -- comrade_executor now can (section 4), and its
  -- policy pins team_id and status but says nothing about user_id.
  if new.team_id is distinct from old.team_id
     or new.user_id is distinct from old.user_id then
    raise exception 'a membership cannot be moved between teams or members';
  end if;

  -- Worker roles have no auth.uid(); their RLS policies are the whole gate.
  if actor is null then
    return new;
  end if;

  if new.status is distinct from old.status then
    if not (
         (old.status = 'invited' and new.status = 'active'
          and actor = new.user_id)
      or (old.status = 'active' and new.status = 'left'
          and actor = new.user_id)
      or (old.status = 'left' and new.status = 'invited'
          and public.is_team_leader(new.team_id))
    ) then
      raise exception 'a membership cannot go from % to % here', old.status,
        new.status;
    end if;
  end if;

  return new;
end $$;

-- ============================================================
-- 3. Departure effects: archive an emptied team, pass leadership on
-- ============================================================
-- The succession half is not a flourish. Without it, D4 ships a trap: the
-- leader is the only one who can invite (server/invites.py) and rename
-- (au_teams_update), so the moment the founding leader leaves, a team with
-- three remaining members can never grow again and nobody can say why.
--
-- Passing the role to the longest-standing remaining member keeps the
-- existing authority shape exactly as it was rather than widening who may
-- invite -- the smaller change, and the one that does not quietly rewrite the
-- permission model as a side effect of adding a leave button.

create or replace function public.trg_membership_departure_effects()
returns trigger language plpgsql security definer set search_path = '' as $$
declare
  still_active boolean;
begin
  select exists (
    select 1 from public.memberships
    where team_id = new.team_id and status = 'active'
  ) into still_active;

  -- Succession first: an emptied team has nobody to promote.
  if still_active and not exists (
    select 1 from public.memberships
    where team_id = new.team_id and status = 'active' and role = 'leader'
  ) then
    update public.memberships set role = 'leader'
    where id = (
      select id from public.memberships
      where team_id = new.team_id and status = 'active'
      -- Longest-standing first. joined_at can be null on rows seeded before
      -- it was written, so fall back to created_at rather than sorting nulls
      -- to the front and handing the team to the newest arrival.
      order by coalesce(joined_at, created_at), created_at, id
      limit 1
    );
  end if;

  update public.teams
     set archived_at = case when still_active then null else now() end
   where id = new.team_id
     -- Only when the state actually changes, so re-reading a settled team
     -- does not keep re-stamping archived_at with a fresh now().
     and (not still_active) is distinct from (archived_at is not null);

  return null;
end $$;

revoke all on function public.trg_membership_departure_effects()
  from public, anon, authenticated;

drop trigger if exists trg_membership_departure on public.memberships;
create trigger trg_membership_departure
  after insert or update of status on public.memberships
  for each row execute function public.trg_membership_departure_effects();

-- ============================================================
-- 4. Who may delete, and who may write 'left'
-- ============================================================
-- DELETE now means "this invitation never happened" and nothing else: the
-- leader withdrawing an invite, or the invitee declining one. Neither is a
-- removal, because nobody has joined yet. An active membership has no DELETE
-- path at all any more -- leaving is an UPDATE, and the row stays.

drop policy if exists au_memberships_delete on public.memberships;
create policy au_memberships_delete on public.memberships for delete to authenticated
  using (
    status = 'invited'
    and (public.is_team_leader(team_id) or user_id = (select auth.uid()))
  );

-- The consent-routed departure (shared/consent.py member_depart) executes as
-- comrade_executor, like every other approved action. This is the narrowest
-- grant that lets it: within the transaction's own team, an ACTIVE row may
-- become a LEFT one, and nothing else. It cannot invite, cannot admit, cannot
-- promote. Moving the row to another person is blocked by the identity guard
-- above, which is why that guard had to stop exempting worker roles.
grant update on public.memberships to comrade_executor;

drop policy if exists ex_memberships_depart on public.memberships;
create policy ex_memberships_depart on public.memberships for update to comrade_executor
  using (team_id = public.current_team() and status = 'active')
  with check (team_id = public.current_team() and status = 'left');

-- ============================================================
-- 5. A team you have left is a team you cannot see
-- ============================================================
-- has_membership (20260831120000) deliberately ignores status, so an INVITED
-- member can read the name of the team inviting them. 'left' must not ride in
-- on that or the team hangs around in the picker forever with no way to open
-- it -- present enough to confuse, useless enough to annoy.

create or replace function public.has_membership(_team_id uuid)
returns boolean language sql stable security definer set search_path = '' as $$
  -- Any LIVE membership row, whatever its status: this is how an invited
  -- member learns the name of the team inviting them. Deliberately weaker
  -- than is_team_member, which requires status = 'active', and deliberately
  -- narrower than "any row at all", which would include the departed.
  select exists (
    select 1 from public.memberships
    where team_id = _team_id and user_id = (select auth.uid())
      and status in ('invited','active')
  );
$$;

-- ============================================================
-- 6. Succession is not somebody changing a role
-- ============================================================
-- trg_membership_role_guard (20260612101500) refuses any role change by a
-- non-leader, and it is right to: no self-promotion, no quiet seizing of the
-- rename and invite powers. But succession trips it, because it fires while
-- the departing leader is mid-transaction and has already stopped being an
-- active leader -- so the team is left with no leader at all and the guard is
-- what prevented the fix.
--
-- The distinction the guard needs is "did a person ask for this, or did the
-- schema do it". pg_trigger_depth() is that signal and cannot be forged: a
-- statement a human issues arrives at this guard at depth 1, and the only
-- writer that reaches it any deeper is the departure trigger, because nothing
-- else in this schema writes memberships.role from inside a trigger. Setting
-- a flag with set_config would have been the usual move and is strictly
-- worse -- any caller can set it before their own UPDATE.

create or replace function public.trg_membership_role_guard()
returns trigger language plpgsql set search_path = '' as $$
begin
  if new.role is distinct from old.role then
    -- Depth > 1 means another trigger issued this, not a person. Today that
    -- is only trg_membership_departure_effects passing the role on when a
    -- leader leaves; see section 3 for why that has to happen at all.
    if pg_trigger_depth() > 1 then
      return new;
    end if;
    if (select auth.uid()) = old.user_id then
      raise exception 'you cannot change your own role';
    end if;
    if not public.is_team_leader(old.team_id) then
      raise exception 'only a team leader may change member roles';
    end if;
  end if;
  return new;
end;
$$;
