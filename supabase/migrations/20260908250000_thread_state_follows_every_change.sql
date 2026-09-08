-- The thread follows the task however the task changed.
--
-- 🔴 THE DEFECT (fix.md F28). `trg_tasks_thread_state` was declared
-- `AFTER INSERT OR UPDATE OF status, thread_id`, and `UPDATE OF` fires on the
-- columns the STATEMENT names — not on what the row ends up being.
--
-- Reassignment updates `assignee_id`. The BEFORE guard
-- (`trg_tasks_confirm_guard`) then resets `status` to 'proposed', because a
-- task handed to somebody else is not a task they have confirmed. The
-- statement never mentioned `status`, so this trigger did not run: the task
-- became proposed and its thread stayed 'done'.
--
-- Which is the exact divergence T22 exists to prevent — the board and the
-- thread giving two answers to "is this done". A trigger that listens for a
-- statement's shape rather than a row's change will always miss the changes
-- something else made.
--
-- Fires on every update now, and decides from the row. Change detection moves
-- INTO the function, where it can compare what actually happened.
drop trigger if exists trg_tasks_thread_state on public.tasks;
create trigger trg_tasks_thread_state
  after insert or update on public.tasks
  for each row execute function public.trg_task_thread_state();

create or replace function public.trg_task_thread_state()
returns trigger language plpgsql security definer set search_path = '' as $$
declare
  _state text;
begin
  if new.thread_id is null then
    return new;
  end if;
  -- Nothing that matters moved: no write. The trigger now sees every update,
  -- so this is what keeps a title edit from touching the thread.
  if tg_op = 'UPDATE'
     and new.status is not distinct from old.status
     and new.thread_id is not distinct from old.thread_id then
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
  where id = new.thread_id and team_id = new.team_id and kind = 'work'
    -- And no write when the thread already says this.
    and work_state is distinct from _state;
  return new;
end;
$$;
