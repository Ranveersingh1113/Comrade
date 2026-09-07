-- A file attached to a thread, and what it is for.
--
-- 🔴 Every uploaded document was TEAM KNOWLEDGE the moment it landed. There
-- was no thread binding and no purpose, and `enqueue_document` compiled
-- whatever was uploaded straight into the team wiki — which every member can
-- read. So attaching a file to a restricted thread, once threads could carry
-- files at all, would have published its contents to people who cannot open
-- that thread. Not by a bug in the compiler: by the compiler working exactly
-- as designed on a document nobody had said was shareable.
--
-- Three purposes, and the default is the narrow one:
--
--   turn_context    -- shown to the model for this piece of work, and that is
--                      all. The default, because a member dropping a file into
--                      a conversation is not publishing it.
--   thread_artifact -- kept with the thread, readable by its participants.
--   team_knowledge  -- compiled into the wiki, readable by the whole team.
--                      Only a member can put a document here, deliberately.
alter table public.documents
  add column if not exists thread_id uuid,
  add column if not exists purpose text not null default 'turn_context';

alter table public.documents drop constraint if exists documents_purpose_check;
alter table public.documents add constraint documents_purpose_check check (
  purpose in ('turn_context', 'thread_artifact', 'team_knowledge')
) not valid;
alter table public.documents validate constraint documents_purpose_check;

alter table public.documents drop constraint if exists documents_thread_fk;
alter table public.documents
  add constraint documents_thread_fk
  foreign key (thread_id, team_id)
  references public.threads (id, team_id) on delete cascade;

create index if not exists idx_documents_thread on public.documents (thread_id)
  where thread_id is not null;

-- Existing rows predate the idea of a purpose. They were uploaded through the
-- team documents screen and compiled into the wiki, so team_knowledge is what
-- they actually are — calling them turn_context would retroactively claim a
-- privacy they never had.
update public.documents set purpose = 'team_knowledge'
 where thread_id is null and purpose = 'turn_context';

-- Visibility follows the thread, exactly as it does for tasks (T22) and
-- messages. A document with no thread is a team document, as before.
drop policy if exists au_documents_select on public.documents;
create policy au_documents_select on public.documents for select to authenticated
  using (
    public.is_team_member(team_id)
    and (
      thread_id is null
      or public.can_access_thread(thread_id, (select auth.uid()))
    )
  );

-- Promotion is a member's act. The agent cannot move a document to
-- team_knowledge: it has no update grant on this table at all, and this
-- policy is what a MEMBER's promotion goes through.
drop policy if exists au_documents_update on public.documents;
create policy au_documents_update on public.documents for update to authenticated
  using (
    public.is_team_member(team_id)
    and (
      thread_id is null
      or public.can_access_thread(thread_id, (select auth.uid()))
    )
  )
  with check (
    public.is_team_member(team_id)
    and (
      thread_id is null
      or public.can_access_thread(thread_id, (select auth.uid()))
    )
  );

-- Who promoted what, and when. A file becoming readable by the whole team is
-- a decision somebody made, and the record of it outlives the row: the same
-- reasoning as thread_participant_events.
create table if not exists public.document_promotions (
  id          uuid primary key default gen_random_uuid(),
  document_id uuid not null,
  team_id     uuid not null references public.teams(id) on delete cascade,
  thread_id   uuid,
  actor_id    uuid,
  from_purpose text not null,
  to_purpose   text not null,
  at          timestamptz not null default now()
);
create index if not exists idx_document_promotions_document
  on public.document_promotions (document_id, at desc);

alter table public.document_promotions enable row level security;
grant select on public.document_promotions to authenticated;
drop policy if exists au_document_promotions_select on public.document_promotions;
create policy au_document_promotions_select
  on public.document_promotions for select to authenticated
  using (public.is_team_member(team_id));

create or replace function public.trg_document_promotion()
returns trigger language plpgsql security definer set search_path = '' as $$
begin
  if new.purpose is distinct from old.purpose then
    insert into public.document_promotions
      (document_id, team_id, thread_id, actor_id, from_purpose, to_purpose)
    select new.id, new.team_id, new.thread_id, auth.uid(), old.purpose, new.purpose
    where exists (select 1 from public.teams where id = new.team_id);
  end if;
  return new;
end;
$$;

drop trigger if exists trg_documents_promotion on public.documents;
create trigger trg_documents_promotion
  after update of purpose on public.documents
  for each row execute function public.trg_document_promotion();
