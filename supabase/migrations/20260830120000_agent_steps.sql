-- §3.2: append_step's `steps = steps || %s::jsonb` rewrote the ENTIRE array on
-- every step, so an N-step turn wrote O(N^2) bytes and left N dead row
-- versions in agent_runs — the audit's highest-bloat table, produced by the
-- function that "violates the connection rule, the batching rule, and the
-- bloat rule simultaneously." Phase 0's pool fixed the connection half; this
-- is the write half: one append is now one INSERT of one row.
--
-- agent_runs.steps and .current_step stay on the table (dropping a column is
-- a harder migration to reverse than adding one, and an unwritten column
-- hurts nothing) but shared/agent_runs.py stops writing them as of this
-- migration. A later phase drops both once nothing reads them.
--
-- SECURITY: agent_steps holds EXACTLY the content 20260830090000_close_
-- agent_runs_leak.sql just closed the leak on — tool args and results from a
-- member's private turn, previously agent_runs.steps under a team-scoped (not
-- thread-scoped) policy any teammate could read. Moving that content to a new
-- table and granting `authenticated` (or any role but comrade_agent) access
-- to it would reopen the identical leak one table over, and the tests that
-- guard agent_runs would never see it happen on this one. So: grant insert,
-- select to comrade_agent ONLY.
--
-- Confirmed on the running local stack, not assumed: `public`'s default
-- privileges (set by the Supabase bootstrap, `pg_default_acl`) grant
-- authenticated/anon full INSERT/SELECT/UPDATE/DELETE on every NEW table in
-- this schema automatically — the same reason 20260830090000 needed an
-- explicit `revoke select ... from authenticated` rather than just dropping
-- the policy. Skipping the grant to `authenticated` is not enough here, the
-- ambient default grant is already there; it has to be revoked, or RLS's
-- default-deny only makes a member's SELECT come back empty instead of
-- raising, and an empty result is a much easier regression to introduce
-- silently than a removed grant.
create table public.agent_steps (
  id         uuid primary key default gen_random_uuid(),
  run_id     uuid not null references public.agent_runs(id) on delete cascade,
  team_id    uuid not null references public.teams(id) on delete cascade,
  seq        int not null,
  type       text not null,
  tool       text,
  args       jsonb,
  response   jsonb,
  text       text,
  created_at timestamptz not null default now(),
  unique (run_id, seq)
);

-- `unique (run_id, seq)` above already IS an index on (run_id, seq) — a unique
-- constraint is implemented as a unique btree index on exactly those columns,
-- in that order, and it serves `where run_id = $1 order by seq` (get_run's
-- read) and the FK's cascade-delete lookup for free. A second, separate
-- index on the same two columns would just be the bloat this migration is
-- about ending, paid again on every insert. New table in the same migration
-- that creates it, so no CONCURRENTLY question either way (that rule is for
-- adding an index to a table that already has concurrent traffic).

-- Undo the ambient default grant described above. No grant at all for
-- authenticated means a member's query fails at InsufficientPrivilege before
-- RLS is even consulted -- same failure mode close_agent_runs_leak.sql
-- established for agent_runs.
revoke all on public.agent_steps from authenticated;

alter table public.agent_steps enable row level security;

grant insert, select on public.agent_steps to comrade_agent;

-- Mirrors ag_agent_runs: comrade_agent operates on rows scoped by
-- current_team(). `for all` is safe even though only insert/select are
-- granted above — Postgres checks table-level privilege before RLS, so an
-- update or delete attempt fails for lack of grant and never reaches this
-- policy.
create policy ag_agent_steps on public.agent_steps for all to comrade_agent
  using (team_id = public.current_team())
  with check (team_id = public.current_team());
