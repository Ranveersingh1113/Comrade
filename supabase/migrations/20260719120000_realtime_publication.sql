-- Publish the tables the frontend subscribes to.
--
-- The product's real-time model is "write the DB first, then push the ping" —
-- but no table had ever been added to the supabase_realtime publication, so
-- postgres_changes subscriptions connected successfully and then stayed
-- permanently silent. Clients were falling back to refetch-on-focus.
--
-- SECURITY: Realtime applies each table's RLS SELECT policy per subscriber
-- before delivering a row, so this grants no visibility that a plain select
-- would not. In particular au_messages_select still confines private-thread
-- messages to their owner — publishing the table does not broadcast them.
--
-- Soft deletes (messages.deleted_scope, documents.deleted_at) travel as UPDATEs
-- and carry the full new row. True DELETEs only happen when a team cascades,
-- and those arrive as primary-key-only payloads that RLS cannot filter; no
-- screen depends on them.

alter publication supabase_realtime add table public.messages;
alter publication supabase_realtime add table public.tasks;
alter publication supabase_realtime add table public.milestones;
alter publication supabase_realtime add table public.consent_queue;
alter publication supabase_realtime add table public.documents;
-- Drives the memory diff card that appears in the group room.
alter publication supabase_realtime add table public.memory_compilations;
