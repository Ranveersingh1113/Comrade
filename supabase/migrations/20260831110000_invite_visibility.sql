-- 🔴 An invited member could not see their own invitation, so nobody could
-- ever join a team. Found by the write-path audit, 2026-08-31.
--
-- au_memberships_select was `is_team_member(team_id)`, and is_team_member
-- requires `status = 'active'`. An invitation is `status = 'invited'`. So:
--
--     to see the row that lets you become active, you had to be active
--
-- A leader invites someone; server/invites.py writes the membership row
-- correctly; the invitee signs in; TeamGate queries their memberships and
-- gets nothing back. The invitation exists and is invisible to the only
-- person who can accept it. Accepting also failed silently rather than
-- loudly: PostgreSQL applies SELECT policies when evaluating an UPDATE's
-- WHERE clause, so the update matched zero rows and returned no error.
--
-- Together with the team-creation bug fixed in 20260831100000, BOTH ways into
-- Comrade were broken: you could not create a team, and you could not accept
-- an invitation to one.
--
-- The fix is the narrowest thing that works: you may always see YOUR OWN
-- membership rows, whatever their status. That reveals no other member — the
-- clause is `user_id = auth.uid()` — and it is how you discover you have been
-- invited.

drop policy if exists au_memberships_select on public.memberships;
create policy au_memberships_select on public.memberships for select to authenticated
  using (
    public.is_team_member(team_id)          -- the roster of a team you are in
    or user_id = (select auth.uid())        -- your own rows, including invites
  );

-- And the team behind the invitation needs a name, or the invitee is offered
-- a blank card. Same shape: a team you hold ANY membership row for, not just
-- an active one.
drop policy if exists au_teams_select on public.teams;
create policy au_teams_select on public.teams for select to authenticated
  using (
    public.is_team_member(id)
    -- INSERT ... RETURNING on creation, before the membership row exists
    -- (20260831100000).
    or created_by = (select auth.uid())
    or exists (
      select 1 from public.memberships m
      where m.team_id = teams.id and m.user_id = (select auth.uid())
    )
  );
