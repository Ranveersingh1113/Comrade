-- Each run's allowance belongs to one hour, and it says which.
--
-- 🔴 (fix.md F44) `claim_budget` updated `usage_buckets` for
-- `date_trunc('hour', now())` and nothing else. Across an hour boundary that
-- row may not exist yet, because buckets are created at admission — so a run
-- admitted at 10:59 asking for another slice at 11:00 matched no row and was
-- REFUSED, with a full unused hour in front of it. If some unrelated request
-- happened to be admitted first, the row existed and the claim succeeded.
-- Whether a member's turn could continue depended on other people's traffic.
--
-- The mirror image is in `finalize_usage`, which skipped reconciliation
-- entirely when `created_at < date_trunc('hour', now())` — so a run that
-- crossed the boundary never gave its unspent estimate back, and the team
-- silently lost budget it had not used.
--
-- THE POLICY, stated rather than emergent: a run's claims are charged to the
-- hour it was ADMITTED in, for the whole life of the run. That hour's cap is
-- the one bounding it, `claim_budget` creates that bucket if it has to, and
-- finalization reconciles the same one. A long turn therefore cannot be
-- refused by a boundary it happened to cross, and cannot move a refund into
-- an hour it did not spend in.
--
-- Nullable and backfilled from `created_at`: runs already in flight when this
-- ships keep exactly the hour they were admitted in, which is the hour their
-- estimate was charged to.
alter table public.agent_runs
  add column if not exists usage_bucket timestamptz;

update public.agent_runs
   set usage_bucket = date_trunc('hour', created_at)
 where usage_bucket is null;

-- No grant needed: `comrade_agent` holds TABLE-level select/insert/update on
-- agent_runs, so a new column is covered already. Checked rather than assumed
-- — the column-level pattern elsewhere in this schema is for roles that were
-- deliberately narrowed, and adding a redundant grant here would imply this
-- table is one of them.
