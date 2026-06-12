-- Comrade — initial schema (structure only; RLS in 0002_rls.sql)
-- Postgres / Supabase. Run order matters (FKs).

-- ============================================================
-- Extensions
-- ============================================================
create extension if not exists vector;      -- pgvector for compiled-memory embeddings

-- ============================================================
-- Shared helpers
-- ============================================================
create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

-- ============================================================
-- A. Identity & membership
-- ============================================================

-- 1 row per auth user. id mirrors auth.users.id.
create table public.profiles (
  id              uuid primary key references auth.users(id) on delete cascade,
  display_name    text not null,
  email           text,
  github_username text,
  created_at      timestamptz not null default now()
);

-- = project = group room (one room per team in v1)
create table public.teams (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  created_by  uuid references public.profiles(id) on delete set null,
  created_at  timestamptz not null default now()
);

-- user <-> team link, with role
create table public.memberships (
  id         uuid primary key default gen_random_uuid(),
  team_id    uuid not null references public.teams(id) on delete cascade,
  user_id    uuid not null references public.profiles(id) on delete cascade,
  role       text not null default 'member' check (role in ('leader','member')),
  status     text not null default 'invited' check (status in ('invited','active')),
  joined_at  timestamptz,
  created_at timestamptz not null default now(),
  unique (team_id, user_id)
);
create index idx_memberships_team on public.memberships(team_id);
create index idx_memberships_user on public.memberships(user_id);

-- ============================================================
-- B. Communication
-- ============================================================

-- group chat + private AI threads, one table.
-- body is ALWAYS untrusted content (injection rule).
create table public.messages (
  id              uuid primary key default gen_random_uuid(),
  team_id         uuid not null references public.teams(id) on delete cascade,
  thread_type     text not null check (thread_type in ('group','private')),
  thread_owner_id uuid references public.profiles(id) on delete cascade,  -- null for group
  sender_kind     text not null check (sender_kind in ('user','ai')),
  sender_id       uuid references public.profiles(id) on delete set null, -- null when ai
  body            text not null,
  deleted_scope   text check (deleted_scope in ('everyone','me')),
  deleted_by      uuid references public.profiles(id) on delete set null,
  deleted_at      timestamptz,
  created_at      timestamptz not null default now(),
  -- private threads must name an owner; group threads must not
  check ((thread_type = 'private') = (thread_owner_id is not null)),
  -- user messages must name a sender; ai messages must not
  check ((sender_kind = 'user') = (sender_id is not null))
);
create index idx_messages_team_thread on public.messages(team_id, thread_type, created_at);
create index idx_messages_private_owner on public.messages(thread_owner_id) where thread_type = 'private';

-- ============================================================
-- C. Documents & memory
-- ============================================================

create table public.documents (
  id           uuid primary key default gen_random_uuid(),
  team_id      uuid not null references public.teams(id) on delete cascade,
  uploader_id  uuid references public.profiles(id) on delete set null,
  kind         text not null check (kind in ('pdf','docx','whatsapp','link','text')),
  filename     text,
  storage_path text,
  status       text not null default 'uploaded' check (status in ('uploaded','parsing','ready','failed')),
  summary      text,
  deleted_at   timestamptz,                                       -- soft delete (any member)
  deleted_by   uuid references public.profiles(id) on delete set null,
  created_at   timestamptz not null default now()
);
create index idx_documents_team on public.documents(team_id);

-- unopened-doc nudge tracking. engagement = card interaction, not raw click.
create table public.document_opens (
  id             uuid primary key default gen_random_uuid(),
  document_id    uuid not null references public.documents(id) on delete cascade,
  user_id        uuid not null references public.profiles(id) on delete cascade,
  first_opened_at timestamptz,
  expanded       boolean not null default false,
  questions_asked integer not null default 0,
  unique (document_id, user_id)
);

-- Project memory = COMPILED artifact, not an authored record.
-- Only the compiler role writes memory_*; the conversational agent reads only.
-- Members never write facts; they trigger reverts via memory_reverts (below).

-- one row per compile run; posts a diff card to group chat (diff_message_id).
create table public.memory_compilations (
  id              uuid primary key default gen_random_uuid(),
  team_id         uuid not null references public.teams(id) on delete cascade,
  trigger         text not null check (trigger in ('scheduled','on_demand','flagged')),
  status          text not null default 'running' check (status in ('running','done','failed')),
  entries_added   integer not null default 0,
  entries_revised integer not null default 0,
  entries_removed integer not null default 0,
  diff_message_id uuid references public.messages(id) on delete set null,
  started_at      timestamptz not null default now(),
  finished_at     timestamptz
);
create index idx_memory_compilations_team on public.memory_compilations(team_id, started_at);

-- stable logical entry — the unit a member reverts. Versions hang off this.
create table public.memory_entries (
  id         uuid primary key default gen_random_uuid(),
  team_id    uuid not null references public.teams(id) on delete cascade,
  archived   boolean not null default false,
  created_at timestamptz not null default now()
);
create index idx_memory_entries_team on public.memory_entries(team_id);

-- immutable compiled versions; bi-temporal validity. is_active = current.
create table public.memory_versions (
  id             uuid primary key default gen_random_uuid(),
  entry_id       uuid not null references public.memory_entries(id) on delete cascade,
  team_id        uuid not null references public.teams(id) on delete cascade,
  compilation_id uuid references public.memory_compilations(id) on delete set null,
  fact           text not null,
  embedding      vector(1536),                                   -- text-embedding-3-small
  change_type    text not null check (change_type in ('added','revised','reverted')),
  is_active      boolean not null default true,
  valid_from     timestamptz not null default now(),
  valid_until    timestamptz,
  created_at     timestamptz not null default now()
);
create index idx_memory_versions_entry on public.memory_versions(entry_id);
create index idx_memory_versions_team on public.memory_versions(team_id);
-- partial HNSW index: only active versions are searched
create index idx_memory_versions_active_embedding on public.memory_versions
  using hnsw (embedding vector_cosine_ops) where is_active;

-- every claim links its source(s). source_id is polymorphic -> validated in app, no FK.
create table public.memory_citations (
  id          uuid primary key default gen_random_uuid(),
  version_id  uuid not null references public.memory_versions(id) on delete cascade,
  source_kind text not null check (source_kind in ('message','document','github')),
  source_id   uuid not null,
  excerpt     text,
  created_at  timestamptz not null default now()
);
create index idx_memory_citations_version on public.memory_citations(version_id);

-- member-triggered reverts. Members write HERE, never to memory itself.
-- compiler/app honours these (deactivate version, reactivate prior).
create table public.memory_reverts (
  id                  uuid primary key default gen_random_uuid(),
  entry_id            uuid not null references public.memory_entries(id) on delete cascade,
  team_id             uuid not null references public.teams(id) on delete cascade,
  member_id           uuid not null references public.profiles(id) on delete cascade,
  reverted_version_id uuid references public.memory_versions(id) on delete set null,
  created_at          timestamptz not null default now()
);
create index idx_memory_reverts_entry on public.memory_reverts(entry_id);

-- ============================================================
-- D. Tasks & deadlines
-- ============================================================

-- not live until confirmed_at set by the assignee.
create table public.tasks (
  id              uuid primary key default gen_random_uuid(),
  team_id         uuid not null references public.teams(id) on delete cascade,
  assignee_id     uuid references public.profiles(id) on delete set null,
  title           text not null,
  description     text,
  deadline        timestamptz,
  status          text not null default 'proposed'
                    check (status in ('proposed','confirmed','in_progress','done')),
  created_by_kind text not null check (created_by_kind in ('user','ai')),
  created_by_id   uuid references public.profiles(id) on delete set null,
  confirmed_at    timestamptz,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);
create index idx_tasks_team on public.tasks(team_id);
create index idx_tasks_assignee on public.tasks(assignee_id);
create trigger trg_tasks_updated_at before update on public.tasks
  for each row execute function public.set_updated_at();

-- project-level user-set deadlines (separate from per-task deadlines)
create table public.milestones (
  id         uuid primary key default gen_random_uuid(),
  team_id    uuid not null references public.teams(id) on delete cascade,
  title      text not null,
  due_at     timestamptz,
  created_by uuid references public.profiles(id) on delete set null,
  created_at timestamptz not null default now()
);
create index idx_milestones_team on public.milestones(team_id);

-- ============================================================
-- E. GitHub
-- ============================================================

create table public.github_repos (
  id             uuid primary key default gen_random_uuid(),
  team_id        uuid not null references public.teams(id) on delete cascade,
  repo_full_name text not null,                 -- e.g. "owner/repo"
  last_synced_at timestamptz,
  created_at     timestamptz not null default now(),
  unique (team_id, repo_full_name)
);

-- flat repo-graph events (commits/PRs/merges). webhooks update changed nodes only.
-- AI queries this, not the raw repo.
create table public.github_activity (
  id             uuid primary key default gen_random_uuid(),
  team_id        uuid not null references public.teams(id) on delete cascade,
  repo_id        uuid not null references public.github_repos(id) on delete cascade,
  node_type      text not null check (node_type in ('commit','pr','merge')),
  author_github  text,
  author_user_id uuid references public.profiles(id) on delete set null,  -- mapped
  payload        jsonb,
  occurred_at    timestamptz,
  created_at     timestamptz not null default now()
);
create index idx_github_activity_team on public.github_activity(team_id, occurred_at);

-- ============================================================
-- F. Agent operations
-- ============================================================

-- DB-first write, then Realtime ping. hash verified before execute.
create table public.consent_queue (
  id                   uuid primary key default gen_random_uuid(),
  team_id              uuid not null references public.teams(id) on delete cascade,
  requesting_member_id uuid references public.profiles(id) on delete set null,
  tool_name            text not null,
  tool_args            jsonb not null,
  source_snippet       text,
  action_hash          text not null,                       -- hash of name+args at creation
  status               text not null default 'pending'
                         check (status in ('pending','approved','edited','cancelled','executed','rejected')),
  reversible           boolean not null default false,
  expires_at           timestamptz,                          -- TTL (~5 min)
  created_at           timestamptz not null default now(),
  resolved_at          timestamptz
);
create index idx_consent_queue_team_status on public.consent_queue(team_id, status);

-- behavioural eval + crash recovery. write before each tool call, not just start/end.
create table public.agent_runs (
  id            uuid primary key default gen_random_uuid(),
  team_id       uuid not null references public.teams(id) on delete cascade,
  trigger_type  text not null check (trigger_type in ('user','document','scheduled')),
  input_summary text,
  steps         jsonb not null default '[]',   -- ordered: tool, args, consent outcome, wall_time
  current_step  integer not null default 0,
  status        text not null default 'running' check (status in ('running','done','failed')),
  created_at    timestamptz not null default now(),
  finished_at   timestamptz
);
create index idx_agent_runs_team on public.agent_runs(team_id, created_at);

-- document pipeline queue. polling worker.
create table public.jobs (
  id          uuid primary key default gen_random_uuid(),
  team_id     uuid not null references public.teams(id) on delete cascade,
  job_type    text not null check (job_type in ('parse_document','embed','compile_memory')),
  payload     jsonb,
  status      text not null default 'pending' check (status in ('pending','processing','done','failed')),
  attempts    integer not null default 0,
  last_error  text,
  created_at  timestamptz not null default now(),
  picked_at   timestamptz,
  finished_at timestamptz
);
create index idx_jobs_status on public.jobs(status, created_at);

-- ============================================================
-- G. Contribution (computed view; no rankings, no feed)
-- ============================================================
-- scalar subqueries avoid join fan-out across the three signal sources.
create view public.contribution_v as
select
  m.team_id,
  m.user_id,
  (select count(*) from public.tasks t
     where t.team_id = m.team_id and t.assignee_id = m.user_id
       and t.status = 'done')                                   as tasks_done,
  (select count(*) from public.tasks t
     where t.team_id = m.team_id and t.assignee_id = m.user_id
       and t.status in ('confirmed','in_progress','done'))      as tasks_active,
  (select count(*) from public.github_activity g
     where g.team_id = m.team_id and g.author_user_id = m.user_id) as github_events,
  (select count(*) from public.messages msg
     where msg.team_id = m.team_id and msg.thread_type = 'group'
       and msg.sender_kind = 'user' and msg.sender_id = m.user_id
       and msg.deleted_scope is null)                            as group_messages
from public.memberships m
where m.status = 'active';

-- ============================================================
-- H. Change history (audit) — visible change log for team content
-- ============================================================
-- one place for milestones/tasks/documents change history.
-- (memory has its own richer versioning above.)
create table public.change_log (
  id         uuid primary key default gen_random_uuid(),
  team_id    uuid not null references public.teams(id) on delete cascade,
  table_name text not null,
  row_id     uuid not null,
  actor_kind text not null check (actor_kind in ('user','ai','compiler','system')),
  actor_id   uuid references public.profiles(id) on delete set null,
  action     text not null check (action in ('create','update','delete')),
  before     jsonb,
  after      jsonb,
  created_at timestamptz not null default now()
);
create index idx_change_log_row on public.change_log(table_name, row_id);
create index idx_change_log_team on public.change_log(team_id, created_at);
