-- Aggregate spend, for the operations endpoint.
--
-- `/metrics` answers "is the hourly cap set right", which needs the spend
-- across the deployment — a cross-team question, and the control role is the
-- one that asks those. Consistent with the line T26 drew: this role sees
-- METADATA about work and never content. Turn counts and token counts are the
-- same class of fact as the run statuses and job attempts it already reads.
--
-- The endpoint sums these, so no per-team figure leaves the process.
grant select (team_id, bucket, turns, tokens)
  on public.usage_buckets to comrade_control;

drop policy if exists ctl_usage_buckets on public.usage_buckets;
create policy ctl_usage_buckets on public.usage_buckets
  for select to comrade_control using (true);
