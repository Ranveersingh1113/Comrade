-- Existing messages may be large. Keep this statement outside a transaction.
create index concurrently if not exists idx_messages_thread_created
  on public.messages (thread_id, created_at, id);
