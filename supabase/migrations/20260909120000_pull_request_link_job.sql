-- A job type for the one row a published pull request still needs.
--
-- 🔴 (fix.md F41) When correlation failed, the code logged that "a later
-- attempt returns the same PR and writes the mapping then". There is no later
-- attempt: `execute_consent` claims the row `executed` with a CAS BEFORE
-- running the executor, so a retry answers `noop` and the executor never runs
-- again. The pull request stayed open with no thread mapping — and a mapping is
-- what every CI check needs to find the conversation that asked for the work.
--
-- The repair goes on the job queue rather than into a table of its own. That
-- queue already has attempts, backoff and a dedupe key, which is all a repair
-- intent needs; a second mechanism beside it would be a second thing to
-- monitor and a second thing to get wrong.
--
-- Note what this job is NOT allowed to do: it writes one row and calls nothing
-- external. The publication already happened, so a repair that re-ran it would
-- be opening a second pull request for work a member approved once.
alter table public.jobs
  drop constraint if exists jobs_job_type_check;

alter table public.jobs
  add constraint jobs_job_type_check check (job_type = any (array[
    'parse_document',
    'embed',
    'compile_memory',
    'ingest_github',
    'compile_github',
    'sync_repo',
    'build_environment',
    'compact_thread',
    'link_pull_request'
  ]));
