-- What a job is ABOUT, separated from what it carries.
--
-- Two control-plane readers need one field out of `jobs.payload`: the
-- repository a `sync_repo` job names. Restoring `select (payload)` to serve
-- them would hand the cross-team role every webhook body again (see
-- 20260908130000), so the field they actually need becomes a column of its
-- own.
--
-- `subject` is deliberately narrow: the non-sensitive IDENTITY of a job's
-- target, never its content. A repository's full name is already readable by
-- this role through `github_repos.repo_full_name`, so this exposes nothing new
-- — which is the test for whether something belongs here.
alter table public.jobs add column if not exists subject text;

-- Backfill the one job type that has one, so the sweeps keep working across
-- the window where old rows are still queued.
update public.jobs
   set subject = payload->>'repo_full_name'
 where job_type in ('sync_repo', 'build_environment')
   and subject is null
   and payload ? 'repo_full_name';

grant select (subject) on public.jobs to comrade_control;
grant select (subject), insert (subject), update (subject)
  on public.jobs to comrade_pipeline;

create index if not exists idx_jobs_subject
  on public.jobs (team_id, job_type, subject)
  where subject is not null;
