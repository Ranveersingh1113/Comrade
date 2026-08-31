-- 🔴 Realtime was publishing six tables and delivering nothing.
--
-- 20260719120000 added these tables to the supabase_realtime publication and
-- fixed the symptom it was written for: subscriptions that connect and then
-- stay permanently silent. The same symptom came back one layer down, and for
-- a reason that migration's own comment states without following through —
--
--   "Realtime applies each table's RLS SELECT policy per subscriber before
--    delivering a row"
--
-- To do that, Realtime has to evaluate the policy against the row as it
-- appears IN THE WAL, not by reading it back from the table. With the default
-- replica identity the WAL carries only the primary key, so the policy has no
-- team_id to test, the check cannot pass, and the row is dropped. Silently:
-- the subscription is healthy, the INSERT succeeds, and no event ever arrives.
--
-- Found by tests/integration/realtime.test.ts after a from-scratch stack
-- rebuild pulled a newer realtime image (v2.129.3). It failed identically on
-- master, which is what ruled out the change being tested at the time — this
-- is not a local quirk but the behaviour any deploy on this version gets.
--
-- The blast radius was every realtime feature: the group room, the task board,
-- the consent inbox badge, the document list and the memory diff card were all
-- falling back to refetch-on-focus, which looks like "a bit laggy" rather than
-- "broken", which is why it went unnoticed.
--
-- COST: with FULL, an UPDATE or DELETE writes the entire old row to the WAL
-- rather than just the key. These six tables are small and text-shaped, and
-- the alternative is a feature that does not work. `messages.body` is the
-- largest column and is exactly the thing a subscriber is waiting for.
--
-- Only these six. Adding a table to the publication without this is the same
-- bug again, so tests/test_migration_realtime_publication.py now asserts the
-- two together rather than the publication alone.

alter table public.messages            replica identity full;
alter table public.tasks               replica identity full;
alter table public.milestones          replica identity full;
alter table public.consent_queue       replica identity full;
alter table public.documents           replica identity full;
alter table public.memory_compilations replica identity full;
