-- The sweep has to compare the cursor it is actually resuming from.
--
-- 🔴 Capture resumes from the compound cursor `(created_at, id)` — timestamps
-- are not unique, and a batch boundary can fall inside a group that shares
-- one. The candidate sweep compared TIMESTAMPS ONLY, so it excluded every row
-- at the boundary timestamp and never asked for another job; those messages
-- waited for unrelated later chatter, which on a quiet team is forever
-- (fix.md F15).
--
-- Fixing that means the sweep needs `messages.id`, and the sweep runs as
-- `comrade_control`, which had column grants for `created_at`, `team_id`,
-- `thread_id`, `sender_kind` and `deleted_scope` — deliberately not `body`.
--
-- An id is the same class of thing as those: it identifies a row and says
-- nothing about what is in it. This role can already count messages and see
-- when they arrived; being able to order them stably adds no content and is
-- the difference between a cursor that works and one that stalls.
grant select (id) on public.messages to comrade_control;

-- And the other half of the same cursor. `comrade_control` could read
-- `chat_through` and not `chat_through_id`, which is exactly the timestamp-only
-- view that made the sweep skip tied rows.
grant select (chat_through_id) on public.memory_compilations to comrade_control;
