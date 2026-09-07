-- A task and the thread where its work happens.
--
-- 🔴 They were unconnected. `tasks` had one status vocabulary
-- (proposed/confirmed/in_progress/done) and `threads.work_state` had another
-- (planned/active/waiting/review/done), and nothing joined them — so a team
-- doing a piece of work in a thread ALSO had a task about it, and the two
-- disagreed the moment either moved. There was no answer to "is this done",
-- only two answers.
--
-- The TASK is the unit of work: it has an owner, a deadline, and a human who
-- decides when it is finished. A work thread is where that work is carried
-- out. So the task's status is authoritative, and a linked thread's
-- `work_state` follows it rather than competing with it.
alter table public.tasks
  add column if not exists thread_id uuid;

alter table public.tasks drop constraint if exists tasks_thread_fk;
alter table public.tasks
  add constraint tasks_thread_fk
  foreign key (thread_id, team_id)
  references public.threads (id, team_id) on delete set null;

create index if not exists idx_tasks_thread on public.tasks (thread_id)
  where thread_id is not null;

-- 🔴 A LEAK THE LINK WOULD HAVE CREATED. `au_tasks_select` is
-- `is_team_member(team_id)` — every member sees every task. Link a task to a
-- RESTRICTED thread and its title, owner and deadline become visible to
-- people who cannot open the thread, which is exactly the "including
-- counts/previews" the plan warns about. A task in a restricted thread is
-- visible only to that thread's participants.
drop policy if exists au_tasks_select on public.tasks;
create policy au_tasks_select on public.tasks for select to authenticated
  using (
    public.is_team_member(team_id)
    and (
      thread_id is null
      or public.can_access_thread(thread_id, (select auth.uid()))
    )
  );

drop policy if exists au_tasks_update on public.tasks;
create policy au_tasks_update on public.tasks for update to authenticated
  using (
    public.is_team_member(team_id)
    and (
      thread_id is null
      or public.can_access_thread(thread_id, (select auth.uid()))
    )
  );

-- One status, one writer. A linked thread's work_state is DERIVED from the
-- task rather than set beside it: two columns that both claim to say whether
-- work is finished will disagree, and the disagreement surfaces as a board
-- that contradicts the thread it links to.
create or replace function public.trg_task_thread_state()
returns trigger language plpgsql security definer set search_path = '' as $$
declare
  _state text;
begin
  if new.thread_id is null then
    return new;
  end if;
  _state := case new.status
    when 'proposed' then 'planned'
    when 'confirmed' then 'planned'
    when 'in_progress' then 'active'
    when 'done' then 'done'
    else 'active'
  end;
  update public.threads
  set work_state = _state, updated_at = now()
  where id = new.thread_id and team_id = new.team_id and kind = 'work';
  return new;
end;
$$;

drop trigger if exists trg_tasks_thread_state on public.tasks;
create trigger trg_tasks_thread_state
  after insert or update of status, thread_id on public.tasks
  for each row execute function public.trg_task_thread_state();
