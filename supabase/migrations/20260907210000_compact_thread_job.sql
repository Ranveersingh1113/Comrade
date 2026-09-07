-- Thread compaction is a job type, so the queue has to admit it.
--
-- The check constraint is the reason a handler registry and the database
-- agree on what work exists (tests/test_worker_handlers.py compares them in a
-- subprocess). Widening only.
alter table public.jobs drop constraint if exists jobs_job_type_check;
alter table public.jobs add constraint jobs_job_type_check check (
  job_type in (
    'parse_document', 'embed', 'compile_memory', 'ingest_github',
    'compile_github', 'sync_repo', 'build_environment', 'compact_thread'
  )
) not valid;
alter table public.jobs validate constraint jobs_job_type_check;
