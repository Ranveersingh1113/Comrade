-- The audit log has to honour the boundary the rows it copies live behind.
--
-- 🔴 THE DEFECT (fix.md F19). `trg_audit` writes `to_jsonb(new)` — the ENTIRE
-- row — into `change_log.before`/`after`, and `change_log`'s select policy is
-- `is_team_member(team_id)`: team-wide, with no thread scoping at all.
--
-- T22 gave tasks a thread and T24 gave documents one, so a task in a
-- restricted thread and a file attached to a private conversation are both
-- denied to a non-participant by their own policies — and both were readable
-- in full through the audit table by any member of the team. Including
-- `documents.parsed_text`, which is the document's entire contents, and
-- `tasks.description`.
--
-- Two changes, because either alone is insufficient:
--
--   1. The snapshot records WHICH THREAD its row belonged to, and the select
--      policy scopes on it exactly as the source tables do.
--   2. The heaviest content column is not snapshotted at all. An audit of a
--      document is about what happened to it — uploaded, promoted, failed,
--      deleted — not about republishing its text into a second table with its
--      own access rules.

alter table public.change_log add column if not exists thread_id uuid;

create index if not exists idx_change_log_thread
  on public.change_log (thread_id) where thread_id is not null;

-- Already-written records, not only new ones. A backfill from the snapshot
-- itself: the thread id is in there, which is precisely the problem.
update public.change_log
   set thread_id = coalesce(
         (after ->> 'thread_id')::uuid, (before ->> 'thread_id')::uuid)
 where thread_id is null
   and coalesce(after ->> 'thread_id', before ->> 'thread_id') is not null;

-- And the content already published into the audit table has to go too.
-- Nulled rather than the rows deleted: what happened, when, and by whom is
-- the evidence; the document's text was never the evidence.
update public.change_log
   set before = before - 'parsed_text',
       after  = after  - 'parsed_text'
 where table_name = 'documents'
   and (before ? 'parsed_text' or after ? 'parsed_text');

create or replace function public.trg_audit()
returns trigger language plpgsql security definer set search_path = '' as $$
declare
  v_actor_id   uuid := (select auth.uid());
  v_actor_kind text;
  v_team       uuid;
  v_row        uuid;
  v_thread     uuid;
  v_before     jsonb;
  v_after      jsonb;
  v_action     text;
begin
  if v_actor_id is not null then
    v_actor_kind := 'user';
  else
    v_actor_kind := coalesce(nullif(current_setting('app.actor_kind', true), ''), 'system');
  end if;

  if tg_op = 'DELETE' then
    v_team := old.team_id; v_row := old.id; v_before := to_jsonb(old); v_after := null;
    v_action := 'delete';
  elsif tg_op = 'UPDATE' then
    v_team := new.team_id; v_row := new.id; v_before := to_jsonb(old); v_after := to_jsonb(new);
    v_action := 'update';
  else
    v_team := new.team_id; v_row := new.id; v_before := null; v_after := to_jsonb(new);
    v_action := 'create';
  end if;

  -- Which thread this row belonged to, if its table has the notion. `->>` on a
  -- table without the column is null, so this stays generic across the three
  -- tables the trigger serves.
  v_thread := coalesce(
    (v_after ->> 'thread_id')::uuid, (v_before ->> 'thread_id')::uuid);

  -- 🔴 The document's whole text was being copied into a second table with
  -- different access rules. An audit says what happened to a document, not
  -- what was in it.
  v_before := v_before - 'parsed_text';
  v_after  := v_after  - 'parsed_text';

  -- skip when the parent team is gone (e.g. cascade delete of the whole team):
  -- the team's change_log is being cascade-removed too, and there is nothing to
  -- attribute the row to.
  if v_team is not null and exists (select 1 from public.teams where id = v_team) then
    insert into public.change_log
      (team_id, table_name, row_id, thread_id, actor_kind, actor_id, action,
       before, after)
    values
      (v_team, tg_table_name, v_row, v_thread, v_actor_kind, v_actor_id,
       v_action, v_before, v_after);
  end if;

  if tg_op = 'DELETE' then return old; else return new; end if;
end;
$$;

-- The same shape the source tables use: a row with no thread is team-wide,
-- a row with one is visible to whoever can open that thread.
drop policy if exists au_change_log_select on public.change_log;
create policy au_change_log_select on public.change_log
  for select to authenticated
  using (
    public.is_team_member(team_id)
    and (thread_id is null
         or public.can_access_thread(thread_id, (select auth.uid())))
  );
