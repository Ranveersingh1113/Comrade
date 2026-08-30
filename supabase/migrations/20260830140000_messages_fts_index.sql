-- The index that makes messages_search possible (findings §3.1, §19-15).
--
-- GIN over a to_tsvector EXPRESSION rather than a stored tsvector column: no
-- schema change to `messages`, no trigger to keep a column in sync, and the
-- expression indexed is exactly the one the query uses. The cost is that the
-- query must repeat the expression verbatim or the planner will not use it.
--
-- CONCURRENTLY per the migration policy (§8) — `messages` is an existing table.
-- This file must run outside a transaction.

create index concurrently if not exists idx_messages_fts
  on public.messages using gin (to_tsvector('english', body));
