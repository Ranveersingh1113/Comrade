-- Completes the recursion fix. 20260831120000 moved the teams-side lookup into
-- a SECURITY DEFINER helper; the memberships-side lookup needs the same
-- treatment, and for the same reason.
--
-- au_memberships_insert (20260831100000) checks the founding case with two
-- inline subqueries — one on public.teams, one on public.memberships. Both are
-- evaluated under RLS, so the policy on memberships ends up reading
-- memberships, and Postgres refuses the cycle outright rather than reasoning
-- about whether it would terminate.
--
-- The rule this codebase already follows: a policy that must read an
-- RLS-protected table does it through a SECURITY DEFINER function.
-- is_team_member, is_team_leader and shares_team are all built that way, which
-- is exactly why they never hit this.

create or replace function public.is_unfounded_team(_team_id uuid)
returns boolean language sql stable security definer set search_path = '' as $$
  -- True only for a team the caller created that nobody has joined yet: the
  -- one moment at which inserting your own membership is legitimate.
  -- Both halves matter. "No active members" stops a departed member walking
  -- back in; "you created it" stops anyone seeding someone else's empty team.
  select exists (
           select 1 from public.teams t
           where t.id = _team_id and t.created_by = (select auth.uid())
         )
     and not exists (
           select 1 from public.memberships m
           where m.team_id = _team_id and m.status = 'active'
         );
$$;

revoke all on function public.is_unfounded_team(uuid) from public, anon;
grant execute on function public.is_unfounded_team(uuid) to authenticated;

drop policy if exists au_memberships_insert on public.memberships;
create policy au_memberships_insert on public.memberships for insert to authenticated
  with check (
    public.is_team_leader(team_id)                     -- inviting, per server/invites.py
    or (
      user_id = (select auth.uid())
      and public.is_unfounded_team(team_id)            -- founding your own team
    )
  );
