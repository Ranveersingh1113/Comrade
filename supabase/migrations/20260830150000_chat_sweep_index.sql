-- Serves the ambient chat sweep (pipeline/chat.py:sweep_chat_compiles), which
-- findings §3.2 names "the worst query in the codebase": it runs every 5
-- SECONDS from worker.tick() and did a cross-team sequential scan of the whole
-- messages table plus a correlated subquery PER ROW. Measured on 6k messages
-- before this change: Seq Scan, SubPlan executed 6001 times, 12,101 buffers.
--
-- Partial: the sweep only ever looks at undeleted human messages in group
-- rooms, so the index carries only those rows and stays far smaller than the
-- table. The leading team_id supports the per-team grouping and the trailing
-- created_at supports the watermark comparison — equality then range, which is
-- the same shape as the textbook idx_messages_team_thread.
--
-- CONCURRENTLY per the migration policy (§8) — messages is an existing table.

create index concurrently if not exists idx_messages_group_human
  on public.messages (team_id, created_at)
  where thread_type = 'group' and sender_kind = 'user' and deleted_scope is null;
