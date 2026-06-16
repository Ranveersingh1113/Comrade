-- Comrade — data-layer invariants (triggers).
-- RLS decides WHO may touch a row; these enforce VALID TRANSITIONS and audit.

-- ============================================================
-- 1. Task confirm guard — no task goes live without the assignee's confirm
-- ============================================================
create or replace function public.trg_tasks_confirm_guard()
returns trigger language plpgsql set search_path = '' as $$
begin
  if tg_op = 'INSERT' then
    if new.status <> 'proposed' or new.confirmed_at is not null then
      raise exception 'a new task must start as proposed and unconfirmed';
    end if;
    return new;
  end if;

  -- reassignment voids any prior confirmation
  if new.assignee_id is distinct from old.assignee_id then
    new.status := 'proposed';
    new.confirmed_at := null;
  end if;

  -- going live (leaving proposed) or confirming requires the assignee themselves
  if (old.status = 'proposed' and new.status <> 'proposed')
     or (old.confirmed_at is null and new.confirmed_at is not null) then
    if (select auth.uid()) is null or (select auth.uid()) <> new.assignee_id then
      raise exception 'only the assignee may confirm their own task';
    end if;
  end if;

  return new;
end;
$$;

create trigger trg_tasks_confirm
  before insert or update on public.tasks
  for each row execute function public.trg_tasks_confirm_guard();

-- ============================================================
-- 2. Membership role guard — no self-promotion; only a leader changes roles
-- ============================================================
create or replace function public.trg_membership_role_guard()
returns trigger language plpgsql set search_path = '' as $$
begin
  if new.role is distinct from old.role then
    if (select auth.uid()) = old.user_id then
      raise exception 'you cannot change your own role';
    end if;
    if not public.is_team_leader(old.team_id) then
      raise exception 'only a team leader may change member roles';
    end if;
  end if;
  return new;
end;
$$;

create trigger trg_memberships_role
  before update on public.memberships
  for each row execute function public.trg_membership_role_guard();

-- ============================================================
-- 3. Audit — visible change history for team content (tasks/milestones/documents)
--    SECURITY DEFINER so the log always lands regardless of caller RLS/grants.
-- ============================================================
create or replace function public.trg_audit()
returns trigger language plpgsql security definer set search_path = '' as $$
declare
  v_actor_id   uuid := (select auth.uid());
  v_actor_kind text;
  v_team       uuid;
  v_row        uuid;
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

  -- skip when the parent team is gone (e.g. cascade delete of the whole team):
  -- the team's change_log is being cascade-removed too, and there is nothing to
  -- attribute the row to.
  if v_team is not null and exists (select 1 from public.teams where id = v_team) then
    insert into public.change_log
      (team_id, table_name, row_id, actor_kind, actor_id, action, before, after)
    values
      (v_team, tg_table_name, v_row, v_actor_kind, v_actor_id, v_action, v_before, v_after);
  end if;

  if tg_op = 'DELETE' then return old; else return new; end if;
end;
$$;

create trigger trg_tasks_audit
  after insert or update or delete on public.tasks
  for each row execute function public.trg_audit();
create trigger trg_milestones_audit
  after insert or update or delete on public.milestones
  for each row execute function public.trg_audit();
create trigger trg_documents_audit
  after insert or update or delete on public.documents
  for each row execute function public.trg_audit();
