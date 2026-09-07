-- A capture boundary that cannot lose a record.
--
-- 🔴 The watermark was `chat_through`, a bare timestamp, and the sweep asked
-- for `created_at > since`. Two messages sharing a timestamp at the boundary
-- meant one was captured and the other skipped FOREVER — the same defect T09
-- fixed in the message list, here in the path that decides what a team
-- remembers. Timestamps are not unique: two people answering at once, or one
-- transaction inserting several, share them routinely.
--
-- The id is the tiebreak, exactly as it is in the message cursor.
alter table public.memory_compilations
  add column if not exists chat_through_id uuid;

comment on column public.memory_compilations.chat_through_id is
  'Message id at the chat_through boundary. With chat_through it forms the '
  'keyset (created_at, id) the next capture resumes from.';
