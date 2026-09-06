-- A preview that appears only on refresh is a preview nobody uses.
--
-- Caught by the production build rather than by anyone noticing: the Realtime
-- hook's table union is typed to the tables actually in the publication, so
-- subscribing to one that is not simply does not compile. Adding the table to
-- the union without this migration would compile and then silently deliver
-- nothing — a server would come up and the thread would keep saying STARTING
-- until someone reloaded.
alter publication supabase_realtime add table public.sandbox_processes;

-- REPLICA IDENTITY FULL, for the same reason the other subscribed tables carry
-- it: without it an UPDATE's old row arrives with only the primary key, and
-- Realtime's row-level filters — `thread_id=eq.<id>` here — cannot be applied
-- to a payload that does not contain the column being filtered on.
alter table public.sandbox_processes replica identity full;
