-- Which thread a pull request came from, and what CI said about it.
--
-- 🔴 Nothing recorded either. A pull request Comrade opened returned its
-- number to the consent flow and was then forgotten: no row, no thread
-- mapping, nothing. So when GitHub reported that the checks had failed, there
-- was no way to say WHOSE work had failed — the delivery was ingested as
-- repository activity for the wiki and the thread that produced the change
-- never heard about it. A member had to go and look.
create table if not exists public.github_pull_requests (
  id            uuid primary key default gen_random_uuid(),
  team_id       uuid not null references public.teams(id) on delete cascade,
  repo_full_name text not null,
  pr_number     integer not null,
  branch        text not null,
  -- The thread whose work this is. Nullable because a pull request can in
  -- principle arrive another way, and a row that cannot say is better than a
  -- row that guesses.
  thread_id     uuid,
  action_hash   text,
  head_sha      text,
  opened_at     timestamptz not null default now(),
  unique (team_id, repo_full_name, pr_number)
);
create index if not exists idx_github_prs_branch
  on public.github_pull_requests (repo_full_name, branch);

-- One row per check result we have been told about.
--
-- `delivery_id` is unique so a REDELIVERY is a no-op. The job queue already
-- dedupes an active delivery, but only while the job is pending or
-- processing: a manual redelivery from GitHub's UI after the first one
-- finished would otherwise be ingested twice.
create table if not exists public.github_check_results (
  id            uuid primary key default gen_random_uuid(),
  team_id       uuid not null references public.teams(id) on delete cascade,
  repo_full_name text not null,
  pull_request_id uuid references public.github_pull_requests(id) on delete cascade,
  thread_id     uuid,
  head_sha      text not null,
  check_name    text not null,
  status        text not null,
  conclusion    text,
  details_url   text,
  -- True when this result is about a commit the branch has moved past. Kept,
  -- not dropped: it is real history and belongs in the audit, but it must not
  -- drive a decision about work that has since changed.
  stale         boolean not null default false,
  delivery_id   text,
  reported_at   timestamptz not null default now(),
  unique (delivery_id)
);
create index if not exists idx_check_results_thread
  on public.github_check_results (thread_id, reported_at desc);
create index if not exists idx_check_results_sha
  on public.github_check_results (repo_full_name, head_sha);

alter table public.github_pull_requests enable row level security;
alter table public.github_check_results enable row level security;

grant select on public.github_pull_requests to authenticated;
grant select on public.github_check_results to authenticated;

-- Visible to whoever can reach the thread the work belongs to; a row with no
-- thread is team-wide, like a repository fact.
drop policy if exists au_github_prs_select on public.github_pull_requests;
create policy au_github_prs_select on public.github_pull_requests
  for select to authenticated
  using (
    public.is_team_member(team_id)
    and (thread_id is null
         or public.can_access_thread(thread_id, (select auth.uid())))
  );

drop policy if exists au_check_results_select on public.github_check_results;
create policy au_check_results_select on public.github_check_results
  for select to authenticated
  using (
    public.is_team_member(team_id)
    and (thread_id is null
         or public.can_access_thread(thread_id, (select auth.uid())))
  );

grant select, insert, update on public.github_pull_requests to comrade_executor;
grant select, insert, update on public.github_pull_requests to comrade_pipeline;
grant select, insert, update on public.github_check_results to comrade_pipeline;

drop policy if exists ex_github_prs on public.github_pull_requests;
create policy ex_github_prs on public.github_pull_requests
  for all to comrade_executor, comrade_pipeline
  using (team_id = public.current_team()) with check (team_id = public.current_team());

drop policy if exists ex_check_results on public.github_check_results;
create policy ex_check_results on public.github_check_results
  for all to comrade_pipeline
  using (team_id = public.current_team()) with check (team_id = public.current_team());
