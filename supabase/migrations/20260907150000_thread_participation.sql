-- Who joined a thread, who removed them, and when.
--
-- 🔴 `thread_participants` records `added_by` and `joined_at`, so an addition
-- is audited. A REMOVAL deletes the row, taking the only evidence with it —
-- and removal is the consequential half: it revokes a person's access to a
-- thread's whole history, its runs, its approvals and its previews. Nothing
-- recorded who did that or when.
create table if not exists public.thread_participant_events (
  id         uuid primary key default gen_random_uuid(),
  -- No foreign key to threads ON PURPOSE, following sandbox_cleanup: the
  -- evidence has to outlive the row. A cascading thread delete would
  -- otherwise erase the record of every removal that led up to it.
  thread_id  uuid not null,
  team_id    uuid not null references public.teams(id) on delete cascade,
  user_id    uuid not null,
  actor_id   uuid,
  action     text not null check (action in ('added', 'removed')),
  at         timestamptz not null default now()
);
create index if not exists idx_thread_participant_events_thread
  on public.thread_participant_events (thread_id, at desc);

create or replace function public.trg_thread_participant_event()
returns trigger language plpgsql security definer set search_path = '' as $$
begin
  if tg_op = 'INSERT' then
    insert into public.thread_participant_events
      (thread_id, team_id, user_id, actor_id, action)
    values (new.thread_id, new.team_id, new.user_id, new.added_by, 'added');
    return new;
  end if;
  -- Guarded on the team still existing. Deleting a TEAM cascades to its
  -- participants, and this trigger would then try to attach an audit row to
  -- the team that is being removed — a foreign key violation that would make
  -- deleting a team impossible. A team going away takes its audit with it,
  -- which is the right answer for a tenant erasing itself; a THREAD going away
  -- does not, which is why there is no foreign key to threads.
  insert into public.thread_participant_events
    (thread_id, team_id, user_id, actor_id, action)
  select old.thread_id, old.team_id, old.user_id, auth.uid(), 'removed'
  where exists (select 1 from public.teams where id = old.team_id);
  return old;
end;
$$;

drop trigger if exists trg_thread_participants_audit on public.thread_participants;
create trigger trg_thread_participants_audit
  after insert or delete on public.thread_participants
  for each row execute function public.trg_thread_participant_event();

alter table public.thread_participant_events enable row level security;
grant select on public.thread_participant_events to authenticated;

-- Visible to people who can currently reach the thread. Someone removed from a
-- thread loses the history AND the record of their removal, which is the same
-- rule the rest of the thread follows rather than an exception to it.
drop policy if exists au_thread_participant_events_select
  on public.thread_participant_events;
create policy au_thread_participant_events_select
  on public.thread_participant_events for select to authenticated
  using (
    public.is_team_member(team_id)
    and public.can_access_thread(thread_id, (select auth.uid()))
  );

-- 🔴 Neither threads nor their rosters were published, so a teammate creating
-- a thread, renaming one, or adding somebody to one was invisible until a
-- reload. The list refetched on window focus and nowhere else.
--
-- RLS applies per subscriber before delivery, so publishing these grants no
-- visibility a plain select would not: a restricted thread stays invisible to
-- everyone but its participants.
alter publication supabase_realtime add table public.threads;
alter publication supabase_realtime add table public.thread_participants;
