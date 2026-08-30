-- Repository activity becomes wiki facts (findings §16.6 / §22.3).
--
-- Two things the compile path needs that the ingest path did not.

-- ---- 1. its own watermark ----
-- memory_compilations already has chat_through. Sharing it would be a silent
-- data-loss bug in both directions: a repo compile recording a chat_through
-- would advance the chat debounce past messages nobody ever compiled, and a
-- chat compile would do the same to the repo backlog. They are different
-- sources consumed at different rates, so they get different columns.
--
-- The value is max(github_activity.created_at), not occurred_at: created_at is
-- OUR insertion time and only ever moves forward, whereas occurred_at is
-- GitHub's and is nullable — a delivery that arrives late with an older
-- occurred_at would fall behind an advanced watermark and never compile.
--
-- A plain `add column` on an existing table takes only a metadata lock and
-- rewrites nothing (Postgres 11+, nullable, no default), so per the §8
-- migration policy this needs no NOT VALID / CONCURRENTLY treatment.
alter table public.memory_compilations
  add column github_through timestamptz;

comment on column public.memory_compilations.github_through is
  'Repo compiles only: max github_activity.created_at consumed by this run (debounce watermark).';

-- ---- 2. a name for the compile work ----
-- Deliberately NOT reused from 'compile_memory': the two carry different
-- payloads, advance different watermarks and fail for different reasons, and
-- an operator triaging a stuck queue should be able to tell them apart.
-- Widening a check constraint on an existing table -> NOT VALID, then VALIDATE
-- as its own statement (§8).
alter table public.jobs drop constraint if exists jobs_job_type_check;
alter table public.jobs add constraint jobs_job_type_check
  check (job_type in ('parse_document','embed','compile_memory',
                      'ingest_github','compile_github'))
  not valid;
alter table public.jobs validate constraint jobs_job_type_check;
