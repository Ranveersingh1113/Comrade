-- A connected repository can now be on disk, and that has to survive a restart.
--
-- `github_repos` recorded WHICH repo a team connected; nothing had ever cloned
-- one, because ingest is webhook-driven and the payload arrives with the
-- delivery. A clone is different: it is state, it is slow, and a worker can die
-- half way through it.
--
-- Kept as a column rather than inferred from the filesystem. "Is this checkout
-- current" answered by stat-ing a directory is a second source of truth that
-- drifts from the first the moment a disk is wiped, a workspace root is
-- repointed, or two API instances disagree about what they can see.

alter table public.github_repos
  add column if not exists last_cloned_at timestamptz;

comment on column public.github_repos.last_cloned_at is
  'When this repository was last fetched into the team workspace '
  '(shared/workspace.py). Null means never cloned.';

-- 'sync_repo' joins the job types. Same queue, same leases, same three
-- attempts, same PermanentJobError split as every other slow fallible thing —
-- pipeline/worker.py is the only complete bounded-retry loop in the codebase
-- and a clone has no business inventing a second one.
alter table public.jobs
  drop constraint if exists jobs_job_type_check;
alter table public.jobs
  add constraint jobs_job_type_check
  check (job_type in ('parse_document','embed','compile_memory',
                      'ingest_github','compile_github','sync_repo'));

-- pl_github_repos is `for all`, but comrade_pipeline held only SELECT — and
-- Postgres checks table-level privilege BEFORE RLS, so the clone's
-- last_cloned_at write failed on the grant with a policy that would have
-- allowed it. The same asymmetry 20260830120000 notes for agent_steps, in the
-- other direction.
--
-- COLUMN-level, not table-level. The pipeline records when it fetched and has
-- no business renaming a repository or repointing a team at a different one:
-- repo_full_name is what decides which checkout directory exists and which
-- remote is cloned, so it stays writable only by a member under
-- au_github_repos_update.
grant update (last_cloned_at) on public.github_repos to comrade_pipeline;
