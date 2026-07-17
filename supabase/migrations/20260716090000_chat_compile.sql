-- Chat->memory: watermark for the debounced group-chat compile path.
-- A chat compilation records the max message created_at it consumed; the next
-- enqueue only considers messages after the latest watermark. Document
-- compilations leave it null.

alter table public.memory_compilations
  add column chat_through timestamptz;

comment on column public.memory_compilations.chat_through is
  'Chat compiles only: max messages.created_at consumed by this run (debounce watermark).';
