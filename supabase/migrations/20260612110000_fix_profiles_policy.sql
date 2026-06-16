-- Fix: the worker profile policies (ag_profiles / pl_profiles) referenced an
-- unqualified `id` inside the subquery, which bound to memberships.id instead of
-- profiles.id — so the EXISTS was never true and every profile was hidden from
-- the worker roles (team_get_state returned no members). Qualify as profiles.id.

drop policy if exists ag_profiles on public.profiles;
create policy ag_profiles on public.profiles for all to comrade_agent
  using (exists (select 1 from public.memberships mm
                 where mm.user_id = profiles.id and mm.team_id = public.current_team()))
  with check (exists (select 1 from public.memberships mm
                 where mm.user_id = profiles.id and mm.team_id = public.current_team()));

drop policy if exists pl_profiles on public.profiles;
create policy pl_profiles on public.profiles for all to comrade_pipeline
  using (exists (select 1 from public.memberships mm
                 where mm.user_id = profiles.id and mm.team_id = public.current_team()))
  with check (exists (select 1 from public.memberships mm
                 where mm.user_id = profiles.id and mm.team_id = public.current_team()));
