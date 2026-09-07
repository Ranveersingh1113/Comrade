-- What the maintenance sweep needs to find a thread that has outgrown its
-- context window.
--
-- comrade_control holds COLUMN-level grants on purpose, so a new sweep is
-- never quietly a new read of somebody's content. These are the two columns
-- thread compaction needs, and nothing else:
--
--   threads.created_by  -- compaction reads messages AS A MEMBER, because
--                          findings §4.1 keeps comrade_agent off `messages`.
--                          The job has to name someone who can see them, and
--                          the thread's creator always can.
--   thread_working_state.summary_through -- where the last summary stopped.
--
-- Deliberately NOT messages.id: the sweep is a prefilter, and compact_thread
-- recomputes the range authoritatively with the full keyset. An approximate
-- count here cannot cause a message to be missed, only a job to be queued
-- that then finds nothing to do.
grant select (created_by) on public.threads to comrade_control;
grant select (thread_id, team_id, summary_through)
  on public.thread_working_state to comrade_control;

drop policy if exists ctl_thread_working_state on public.thread_working_state;
create policy ctl_thread_working_state on public.thread_working_state
  for select to comrade_control using (true);
