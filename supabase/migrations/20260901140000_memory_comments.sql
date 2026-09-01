-- Members cannot write memory. They can argue with it.
--
-- findings §6.3-6: "no conflict state -- disagreement collapses to `revise`,
-- the losing side vanishes." Consolidation resolves every conflict by picking a
-- winner and marking the loser inactive, and the reasoning that produced the
-- disagreement is nowhere. The wiki records what the team currently believes
-- and never why anyone objected. A comment is where the losing side stays
-- visible.
--
-- ITS OWN TABLE, ON PURPOSE
-- --------------------------
-- The obvious shape is a column on memory_versions. That would require giving
-- members write access to the table comrade_pipeline owns alone (§6.0), which
-- is the single constraint the whole memory design rests on -- the compiler is
-- the sole writer, so every fact is spotlighted, cited and revertible. Comments
-- are member-written by construction, so they live somewhere members may write
-- and the memory tables stay untouched.
--
-- THE ANCHOR IS THE ENTRY, NOT THE VERSION
-- ------------------------------------------
-- The whole design decision. A memory_entry is the durable identity of a fact;
-- a memory_version is one statement of it, and consolidation replaces those
-- routinely. Anchored to a version, a comment would detach from the fact it is
-- about at exactly the moment the fact changes -- which is the one moment it
-- matters, because that is when the objection either was heeded or was not.

create table if not exists public.memory_comments (
  id         uuid primary key default gen_random_uuid(),
  entry_id   uuid not null references public.memory_entries(id) on delete cascade,
  -- Denormalised so RLS can scope without reaching through memory_entries on
  -- every row. The trigger below is what keeps it honest.
  team_id    uuid not null references public.teams(id) on delete cascade,
  author_id  uuid not null references public.profiles(id) on delete cascade,
  body       text not null check (length(btrim(body)) > 0),
  created_at timestamptz not null default now()
);

create index if not exists idx_memory_comments_entry
  on public.memory_comments(entry_id);

comment on table public.memory_comments is
  'Member discussion anchored to a memory ENTRY, so it survives the fact being '
  'revised (findings §6.3-6). Members may write here; memory_* stays '
  'comrade_pipeline''s alone.';

alter table public.memory_comments enable row level security;

-- The team reads the disagreement. A comment only its author can see is a
-- private note, and §6.3-6 is about the losing side staying VISIBLE.
create policy au_memory_comments_select on public.memory_comments
  for select to authenticated
  using (public.is_team_member(team_id));

-- Any member may comment, as themselves. Authorship is the point: an anonymous
-- objection is not a position anyone holds.
create policy au_memory_comments_insert on public.memory_comments
  for insert to authenticated
  with check (
    public.is_team_member(team_id) and author_id = (select auth.uid())
  );

-- Only the author withdraws it -- not a teammate, not the leader. An objection
-- somebody else can delete is not an objection, it is a suggestion the majority
-- may erase, which is the collapse §6.3-6 describes moved one level up.
create policy au_memory_comments_delete on public.memory_comments
  for delete to authenticated
  using (author_id = (select auth.uid()));

-- team_id and entry_id arrive together and can disagree. Without this, a member
-- could file a comment naming an entry from a team they are NOT in while
-- claiming a team_id they are, and then read it back through the select policy.
-- Same shape and same reason as trg_memory_citation_source_team.
create or replace function public.trg_memory_comment_entry_team()
returns trigger language plpgsql security definer set search_path = '' as $$
begin
  if not exists (
    select 1 from public.memory_entries e
    where e.id = new.entry_id and e.team_id = new.team_id
  ) then
    raise exception 'comment team must match the entry team';
  end if;
  return new;
end $$;

revoke all on function public.trg_memory_comment_entry_team()
  from public, anon, authenticated;

drop trigger if exists trg_memory_comment_team on public.memory_comments;
create trigger trg_memory_comment_team
  before insert or update on public.memory_comments
  for each row execute function public.trg_memory_comment_entry_team();
