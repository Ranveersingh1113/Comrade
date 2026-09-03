-- An environment a team ASKED for, whose state they can see.
--
-- WHY THIS EXISTS
-- -----------------
-- Dependency installation was briefly automatic: connect a repository, and the
-- next sync ran `pip install` from its manifest. The privilege split was right
-- (network for the install, none for the run, and the model unable to ask for
-- either) and the trigger was wrong.
--
-- Connecting a repository is a READ consent in a member's head — "Comrade can
-- see our code". Running its manifest unattended turns that into "Comrade may
-- execute this repository's dependency graph, with egress", because pip runs
-- setup.py and a build hook runs whatever it likes. Nobody consented to the
-- second thing, and in a product whose thesis is that actions are proposed and
-- approved, an execute-with-network capability arriving as a side effect of a
-- checkbox is the one shape that cannot be defended.
--
-- So it becomes a thing a team turns on, per repository, and can watch.

alter table public.github_repos
  -- THE OPT-IN. False until a leader says otherwise. Nothing installs
  -- anything for a repository where this is false.
  add column if not exists env_enabled boolean not null default false,
  -- 'building' | 'ready' | 'failed'. Null means never attempted.
  --
  -- 'disabled' and 'stale' are deliberately NOT stored. Disabled is
  -- env_enabled = false, and stale is env_key <> the key the current checkout
  -- would produce. Storing either would be a second copy of something already
  -- known, free to drift from it — the same reason last_cloned_at is a column
  -- and "is this checked out" is not.
  add column if not exists env_status text
    check (env_status is null or env_status in ('building','ready','failed')),
  -- WHAT produced the current environment: recipe version, manifest hash,
  -- lockfile hash, and the commit when the repository's own package is
  -- installed. Comparing it to what the checkout would produce now is how
  -- staleness is answered without storing it.
  add column if not exists env_key text,
  -- Why it failed, for a human. A resolution conflict is a fact about the
  -- project and the team is the only party who can act on it.
  add column if not exists env_error text,
  add column if not exists env_updated_at timestamptz;

comment on column public.github_repos.env_enabled is
  'Whether this team has asked Comrade to build a dependency environment for '
  'this repository. Installing runs the repo''s own build hooks with network '
  'access, so it is never implied by connecting.';

-- ============================================================
-- Who may write what
-- ============================================================
-- The pipeline reports STATUS and never grants PERMISSION. Column-level, the
-- same shape as last_cloned_at in 20260902100000: a worker that could set
-- env_enabled could turn on the very capability a member declined, and the
-- distance between "reports what happened" and "decides what may happen" is
-- exactly what a column grant expresses and a table grant does not.
grant update (env_status, env_key, env_error, env_updated_at)
  on public.github_repos to comrade_pipeline;

-- 'build_environment' joins the job types. Same queue, same three attempts,
-- same PermanentJobError split — a dependency install is slow and fallible in
-- precisely the way that loop already handles.
alter table public.jobs
  drop constraint if exists jobs_job_type_check;
alter table public.jobs
  add constraint jobs_job_type_check
  check (job_type in ('parse_document','embed','compile_memory',
                      'ingest_github','compile_github','sync_repo',
                      'build_environment'));

-- ============================================================
-- Turning it on is a leader action, and it is the only new one
-- ============================================================
-- au_github_repos_update already restricts UPDATE to a team leader holding a
-- matching installation, so enabling rides on the policy that governs every
-- other change to this row. No new policy: a second one covering the same
-- table and the same verb is a second place for the rule to be wrong.
--
-- What DOES need saying is that members can read the status. They already can
-- — au_github_repos_select is is_team_member — so the status surface costs no
-- new grant either. Recorded because "no change needed" is worth stating
-- explicitly in a migration whose whole subject is permission.
