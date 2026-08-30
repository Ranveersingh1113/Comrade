-- Repository activity becomes ingestible (findings §16.5).
--
-- §14 records that the schema always assumed this: github_repos and
-- github_activity landed in migration one with the comment "AI queries this,
-- not the raw repo", memory_citations.source_kind already accepts 'github',
-- and contribution_v already counts github_events. What was missing is that
-- NOTHING WRITES THOSE TABLES — which is why the contribution screen's GitHub
-- half has no data. This migration makes writing them possible.

-- ---- node_type: webhooks deliver far more than commit/pr/merge ----
-- Widening a check constraint on an EXISTING table, so per the migration
-- policy (§8): NOT VALID first, then VALIDATE as its own statement.
alter table public.github_activity drop constraint if exists github_activity_node_type_check;
alter table public.github_activity add constraint github_activity_node_type_check
  check (node_type in ('commit','pr','merge','issue','review','comment','branch'))
  not valid;
alter table public.github_activity validate constraint github_activity_node_type_check;

-- ---- the queue needs a name for the ingest work ----
alter table public.jobs drop constraint if exists jobs_job_type_check;
alter table public.jobs add constraint jobs_job_type_check
  check (job_type in ('parse_document','embed','compile_memory','ingest_github'))
  not valid;
alter table public.jobs validate constraint jobs_job_type_check;

-- ---- the pipeline can now write what it ingests ----
-- It held SELECT on github_activity and NOTHING on github_repos, so it could
-- neither record an event nor resolve a repo to its team.
grant insert on public.github_activity to comrade_pipeline;
grant select on public.github_repos    to comrade_pipeline;

create policy pl_github_repos on public.github_repos for all to comrade_pipeline
  using (team_id = public.current_team())
  with check (team_id = public.current_team());

-- ---- members read repository history; they do not author it ----
-- Supabase grants `authenticated` full CRUD on every table by default (see
-- docs/architecture.md), so members currently hold INSERT/UPDATE/DELETE here.
-- github_activity is a compiled record of what GitHub said. A member editing
-- it would be editing history, and a member INSERTING into it would be
-- fabricating repository events that the compiler then turns into cited wiki
-- facts — a way to put words in the repo's mouth. au_github_activity_select
-- stays; everything else goes.
revoke insert, update, delete, truncate, references
  on public.github_activity from authenticated;

-- ---- lookup index ----
-- github_activity is empty in every environment today (nothing has ever
-- written it), so this is a plain CREATE INDEX rather than CONCURRENTLY: the
-- policy exists to avoid locking a table with rows in it, and there are none.
create index if not exists idx_github_activity_lookup
  on public.github_activity (team_id, node_type, occurred_at desc);
