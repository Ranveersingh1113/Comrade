-- A cleanup queue where one stuck row cannot hold the door.
--
-- 🔴 (fix.md F08) `drain_cleanup` took `where done_at is null order by
-- requested_at limit 50`. A row that fails keeps its place at the FRONT of
-- that order forever, so fifty permanently-failing rows consumed the whole
-- batch on every pass and nothing behind them was ever reclaimed.
--
-- And they did fail permanently. The drain removed each network directly,
-- while the preview proxy's endpoint was still attached to it — Docker
-- refuses that, every time, for as long as the proxy exists.
--
-- Two changes, and they are different problems:
--   * ORDER BY attempts first, so a row that has never been tried is never
--     behind one that has. This alone satisfies "a healthy row gets through".
--   * A retry time, so a row that cannot succeed is not retried on every pass
--     forever. Without it the fix above turns a starved queue into a hot loop
--     against a daemon that has already said no.
alter table public.sandbox_cleanup
  add column if not exists next_attempt_at timestamptz not null default now();

-- Matches the drain's predicate and its order, so the queue stays a queue as
-- it grows.
drop index if exists idx_sandbox_cleanup_pending;
create index idx_sandbox_cleanup_pending
  on public.sandbox_cleanup (attempts, requested_at)
  where done_at is null;
