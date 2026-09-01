-- The index behind memory_search (findings §20.4-3).
--
-- Same shape as idx_messages_fts (20260830140000) and for the same reasons: a
-- GIN over the to_tsvector EXPRESSION rather than a stored tsvector column, so
-- there is no schema change to memory_versions and no trigger keeping a column
-- in sync. The cost is that the query must repeat the expression verbatim or
-- the planner cannot match the index at all.
--
-- PARTIAL on is_active. memory_versions is bi-temporal — a revised fact keeps
-- its superseded row — and search must never return one, so the rows the index
-- omits are exactly the rows the query excludes. Smaller index, and the
-- predicate is stated in the one place it cannot drift from.
--
-- CONCURRENTLY per the migration policy (§8): memory_versions is an existing
-- table. This file must run outside a transaction.

create index concurrently if not exists idx_memory_versions_fts
  on public.memory_versions using gin (to_tsvector('english', fact))
  where is_active;
