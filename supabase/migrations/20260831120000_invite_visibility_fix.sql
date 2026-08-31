-- Corrects 20260831110000, which introduced two problems while fixing the
-- invite-visibility one. Both are worth recording because they are the
-- interesting part.
--
-- ============================================================
-- 1. Infinite recursion between two policies
-- ============================================================
-- 20260831110000 gave au_teams_select an inline
-- `exists (select 1 from public.memberships ...)`. That subquery is itself
-- subject to memberships' RLS, and au_memberships_insert's check references
-- public.teams — so teams -> memberships -> teams, and Postgres raised
-- "infinite recursion detected in policy for relation memberships".
--
-- This is exactly why is_team_member / is_team_leader / shares_team are
-- SECURITY DEFINER: a policy that must read another RLS-protected table has
-- to step outside RLS to do it. Following that established pattern rather
-- than inventing a way around it.

create or replace function public.has_membership(_team_id uuid)
returns boolean language sql stable security definer set search_path = '' as $$
  -- ANY membership row, whatever its status — this is how an INVITED member
  -- learns the name of the team inviting them. Deliberately weaker than
  -- is_team_member, which requires status = 'active'.
  select exists (
    select 1 from public.memberships
    where team_id = _team_id and user_id = (select auth.uid())
  );
$$;

revoke all on function public.has_membership(uuid) from public, anon;
grant execute on function public.has_membership(uuid) to authenticated;

drop policy if exists au_teams_select on public.teams;
create policy au_teams_select on public.teams for select to authenticated
  using (
    public.is_team_member(id)
    or created_by = (select auth.uid())   -- INSERT ... RETURNING on creation
    or public.has_membership(id)          -- a team that has invited you
  );

-- ============================================================
-- 2. A protection that turned out to be accidental
-- ============================================================
-- Before 20260831110000, a member could not move their own membership row to
-- another team — `update memberships set team_id = <other>` was refused. By
-- inspection au_memberships_update should have ALLOWED it: its check is
-- `(user_id = auth.uid()) OR is_team_leader(team_id)`, and the new row still
-- has user_id = auth.uid().
--
-- The audit test explained it. PostgreSQL applies SELECT policies when
-- evaluating an UPDATE's WHERE clause, and au_memberships_select was
-- `is_team_member(team_id)` — so the block came from the SELECT policy, not
-- the UPDATE one, and only for rows in teams you were not already in.
--
-- Widening the select policy to fix invites therefore removed a guard nobody
-- had written down, and the write-path audit caught it in the same run that
-- introduced it. A cross-tenant teleport should not depend on a side effect
-- of an unrelated policy, so here it is on purpose.

create or replace function public.trg_membership_identity_guard()
returns trigger language plpgsql security definer set search_path = '' as $$
begin
  -- Worker roles (auth.uid() is null) are unaffected.
  if (select auth.uid()) is null then
    return new;
  end if;
  if new.team_id is distinct from old.team_id
     or new.user_id is distinct from old.user_id then
    raise exception 'a membership cannot be moved between teams or members';
  end if;
  return new;
end $$;

revoke all on function public.trg_membership_identity_guard() from public, anon, authenticated;

create trigger trg_membership_identity
  before update on public.memberships
  for each row execute function public.trg_membership_identity_guard();
