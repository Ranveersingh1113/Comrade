-- Cheap columns that unblock four separate later items. One migration, because
-- they are all `alter table ... add column` on two tables and splitting them
-- would take four locks for the same work.
--
-- findings §3.1 listed these as missing. §23.4-3 promoted the token/cost pair
-- from housekeeping to the BILLING INPUT — a per-team allowance with a hard
-- stop needs a number to count. §25.7 asked for parent_run_id to be taken
-- "during the agent_steps migration" because it is near-zero cost inside a
-- migration that is happening anyway and genuinely painful to retrofit; it is
-- taken one task earlier here, so agent_steps can reference it from the start.

alter table public.agent_runs
  add column if not exists input_tokens  integer,
  add column if not exists output_tokens integer,
  add column if not exists cost_usd      numeric(12, 6),
  add column if not exists parent_run_id uuid references public.agent_runs(id) on delete set null;

-- 'cancelled' completes the terminal set. 'agent' is the trigger type a
-- subagent run will carry — §25.7's one cheap decision, keeping the
-- multi-agent door open without walking through it.
--
-- Both are WIDENINGS of a check constraint on an existing table, so per the
-- migration policy (§8): NOT VALID first, then VALIDATE as its own statement.
-- Constraint names confirmed against pg_constraint before writing.
alter table public.agent_runs drop constraint if exists agent_runs_status_check;
alter table public.agent_runs add constraint agent_runs_status_check
  check (status in ('running','done','failed','cancelled')) not valid;
alter table public.agent_runs validate constraint agent_runs_status_check;

alter table public.agent_runs drop constraint if exists agent_runs_trigger_type_check;
alter table public.agent_runs add constraint agent_runs_trigger_type_check
  check (trigger_type in ('user','document','scheduled','agent')) not valid;
alter table public.agent_runs validate constraint agent_runs_trigger_type_check;

alter table public.consent_queue
  -- batch_id: the batched "Agent Inbox" approval the governance doc calls for
  -- (§3.1, and propose_batch in §5). Nullable — a lone proposal has no batch.
  add column if not exists batch_id uuid,
  -- agent_run_id: traces a proposal back to the turn that produced it (§3.1).
  add column if not exists agent_run_id uuid references public.agent_runs(id) on delete set null,
  -- resolution_reason: the WHY a rejection carries back to the model (§9.3 G3).
  -- Without it a rejection is a status change the agent can never learn from,
  -- so it re-proposes the same thing. This column is the whole feature.
  add column if not exists resolution_reason text;

-- Deliberately NOT adding indexes on parent_run_id or batch_id. Nothing queries
-- either column yet — subagents and the batched inbox are both later phases.
-- An index on an existing table costs a CONCURRENTLY migration of its own under
-- the policy; buy it when there is a query to serve, not before.
