-- 🔴 Two holes in the team boundary, found together while scoping team
-- lifecycle. Both live in the same two policies.
--
-- ============================================================
-- 1. Anyone could join any team by knowing its id.
-- ============================================================
-- au_memberships_insert was:
--     with check ((user_id = auth.uid()) OR is_team_leader(team_id))
--
-- The first branch exists so a member can FOUND a team — TeamGate.tsx creates
-- the team, then inserts itself as leader — but as written it let ANY
-- authenticated user insert themselves into ANY team, as an active member,
-- given only its id.
--
-- Team ids are UUIDs, so not guessable. They are also in every URL
-- (/t/<teamId>/room), in localStorage, and in any link or screenshot a member
-- shares. is_team_member, is_team_leader and shares_team all trust an active
-- membership row, so one insert buys the room, the wiki, tasks, documents and
-- every member's contribution record.
--
-- More reachable than either private-thread leak closed earlier this week:
-- those needed a database role; this needed a logged-in account and a URL.
--
-- The founding case is preserved exactly, and no wider: you may insert
-- YOURSELF into a team YOU created that has NO active members yet. Both
-- clauses matter — "no active members" stops a departed member walking back
-- in, and "you created it" stops anyone seeding someone else's empty team.

drop policy if exists au_memberships_insert on public.memberships;
create policy au_memberships_insert on public.memberships for insert to authenticated
  with check (
    public.is_team_leader(team_id)
    or (
      user_id = (select auth.uid())
      and exists (
        select 1 from public.teams t
        where t.id = team_id and t.created_by = (select auth.uid())
      )
      and not exists (
        select 1 from public.memberships m
        where m.team_id = memberships.team_id and m.status = 'active'
      )
    )
  );

-- ============================================================
-- 2. Nobody could create a team through the UI.
-- ============================================================
-- au_teams_select was `is_team_member(id)`. TeamGate.tsx creates a team with
-- `.insert({...}).select().single()`, which PostgREST issues as
-- INSERT ... RETURNING — and RETURNING requires the new row to pass the
-- SELECT policy too. The creator is not yet a member: their membership row is
-- the NEXT statement.
--
-- So a bare INSERT succeeded while the app's INSERT failed, which is exactly
-- why no test caught it — nothing had ever created a team the way the product
-- does. Team creation is the first thing a new user does.
--
-- The creator may see the team they created. That is not a widening worth
-- worrying about: created_by never changes, and the team's CONTENTS —
-- messages, tasks, wiki, documents — are each gated by their own
-- is_team_member check, not by this one.

drop policy if exists au_teams_select on public.teams;
create policy au_teams_select on public.teams for select to authenticated
  using (public.is_team_member(id) or created_by = (select auth.uid()));
