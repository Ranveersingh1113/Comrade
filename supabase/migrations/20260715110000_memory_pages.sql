-- Wiki-page memory (PromptQL-style pivot, 2026-07-15): facts group into topic
-- pages. Pages are the consolidation context, the member-facing wiki view,
-- and (later) the agent-recall unit. Sole writer stays comrade_pipeline;
-- members and the agent read.

create table public.memory_pages (
  id          uuid primary key default gen_random_uuid(),
  team_id     uuid not null references public.teams(id) on delete cascade,
  title       text not null,
  description text not null default '',
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (team_id, title)
);
create index idx_memory_pages_team on public.memory_pages(team_id);
create trigger trg_memory_pages_updated_at before update on public.memory_pages
  for each row execute function public.set_updated_at();

-- Each logical entry lives on a page. Nullable: pre-page entries render under
-- a virtual "Uncategorized" bucket; on delete set null degrades the same way.
alter table public.memory_entries
  add column page_id uuid references public.memory_pages(id) on delete set null;
create index idx_memory_entries_page on public.memory_entries(page_id);

-- ---- RLS (mirrors the memory_* pattern in 20260612095500_rls.sql) ----
alter table public.memory_pages enable row level security;

-- authenticated: members READ the wiki; nobody authenticated writes pages
create policy au_memory_pages_select on public.memory_pages for select to authenticated
  using (public.is_team_member(team_id));

-- comrade_agent: read-only (no write grant on memory_*, by design)
grant select on public.memory_pages to comrade_agent;
create policy ag_memory_pages on public.memory_pages for all to comrade_agent
  using (team_id = public.current_team()) with check (team_id = public.current_team());

-- comrade_pipeline: sole writer
grant select, insert, update on public.memory_pages to comrade_pipeline;
create policy pl_memory_pages on public.memory_pages for all to comrade_pipeline
  using (team_id = public.current_team()) with check (team_id = public.current_team());
