-- Linking something to a thread requires being able to open that thread.
--
-- 🔴 THE DEFECT (fix.md F29, and the same hole one table over). T22 narrowed
-- `tasks` SELECT and UPDATE to `can_access_thread(...)` and left INSERT on
-- `is_team_member(team_id)`. So a same-team NON-PARTICIPANT who knows a
-- restricted thread's id could insert a task pointing at it — and
-- `trg_task_thread_state`, which is SECURITY DEFINER, then changes that
-- thread's work state. They cannot read the row back, because SELECT is
-- scoped; the side effect on somebody else's private thread lands anyway.
--
-- `documents` had it too, and worse. Its insert check is
-- `is_team_member(team_id) and uploader_id = auth.uid()` with nothing about
-- the thread, so a non-participant could ATTACH A FILE to a restricted
-- conversation. T24 makes an attachment `turn_context` — shown to the model
-- for that thread's work — which turns this from a state-manipulation bug into
-- a way to put chosen text in front of the agent inside a thread you are not
-- in.
--
-- Writing a link is a use of the thread, and the rule for using a thread is
-- already written: `can_access_thread`.
drop policy if exists au_tasks_insert on public.tasks;
create policy au_tasks_insert on public.tasks
  for insert to authenticated
  with check (
    public.is_team_member(team_id)
    and (thread_id is null
         or public.can_access_thread(thread_id, (select auth.uid())))
  );

drop policy if exists au_documents_insert on public.documents;
create policy au_documents_insert on public.documents
  for insert to authenticated
  with check (
    public.is_team_member(team_id)
    and uploader_id = (select auth.uid())
    and (thread_id is null
         or public.can_access_thread(thread_id, (select auth.uid())))
  );
