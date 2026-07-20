-- Reliability and integrity hardening for the asynchronous compiler path.

-- A worker lease lets another worker reclaim a job abandoned by a process
-- crash. `dedupe_key` prevents concurrent event deliveries from enqueuing the
-- same active job more than once.
alter table public.jobs
  add column if not exists lease_expires_at timestamptz,
  add column if not exists dedupe_key text;

create index if not exists idx_jobs_lease_expiry
  on public.jobs(status, lease_expires_at)
  where status = 'processing';

create unique index if not exists idx_jobs_active_dedupe
  on public.jobs(team_id, job_type, dedupe_key)
  where dedupe_key is not null and status in ('pending', 'processing');

-- Page names are treated case-insensitively by the compiler. Re-home any old
-- case-only duplicates before enforcing that invariant at the database layer.
with ranked_pages as (
  select id, team_id,
         first_value(id) over (
           partition by team_id, lower(title) order by id
         ) as canonical_id
  from public.memory_pages
), rehomed_entries as (
  update public.memory_entries e set page_id = r.canonical_id
  from ranked_pages r
  where e.page_id = r.id and r.id <> r.canonical_id
  returning e.id
)
delete from public.memory_pages p
using ranked_pages r
where p.id = r.id and r.id <> r.canonical_id;

create unique index if not exists idx_memory_pages_team_lower_title
  on public.memory_pages(team_id, lower(title));

-- Citations are polymorphic, but must still point to a source in the same
-- team as the version they support. This makes provenance enforceable even
-- when future callers bypass the Python compiler helpers.
create or replace function public.trg_memory_citation_source_team()
returns trigger language plpgsql security definer set search_path = '' as $$
declare
  version_team uuid;
  source_ok boolean;
begin
  select team_id into version_team
  from public.memory_versions where id = new.version_id;

  if version_team is null then
    raise exception 'citation version does not exist';
  end if;

  if new.source_kind = 'message' then
    select exists (
      select 1 from public.messages
      where id = new.source_id and team_id = version_team
    ) into source_ok;
  elsif new.source_kind = 'document' then
    select exists (
      select 1 from public.documents
      where id = new.source_id and team_id = version_team
    ) into source_ok;
  else
    select exists (
      select 1 from public.github_activity
      where id = new.source_id and team_id = version_team
    ) into source_ok;
  end if;

  if not source_ok then
    raise exception 'citation source must belong to the version team';
  end if;
  return new;
end;
$$;

drop trigger if exists trg_memory_citation_source_team on public.memory_citations;
create trigger trg_memory_citation_source_team
  before insert or update on public.memory_citations
  for each row execute function public.trg_memory_citation_source_team();
