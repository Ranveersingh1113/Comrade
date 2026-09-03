-- A GitHub credential that belongs to a team, instead of one that belongs to us.
--
-- WHAT WAS WRONG
-- ----------------
-- pipeline/repo_sync.py:_token_for(team_id, repo_full_name) ignored BOTH
-- arguments and returned one process-wide PAT. So the credential a team's
-- checkout was fetched with was scoped to everything the PAT's owner could
-- reach — every other team's private repositories included. A team leader
-- could insert any `owner/repo` they liked into github_repos and Comrade
-- would clone it for them.
--
-- Every other isolation property in this system — RLS tenancy, per-team
-- workspaces, the container in agent/sandbox.py — was undone by that one
-- function. It is the reason this migration exists before anything else in
-- the GitHub work.
--
-- WHY AN INSTALLATION RATHER THAN A STORED TOKEN
-- -----------------------------------------------
-- A GitHub App installation is granted a SET OF REPOSITORIES by whoever
-- installed it. The token we mint from it is scoped to exactly that set, and
-- expires in an hour. So the authorization is enforced by GitHub, on every
-- request, rather than by us remembering to check.
--
-- That is what makes the remaining hole harmless: a leader can still type any
-- repository name into github_repos, but the token minted for THEIR
-- installation will not open somebody else's repository. The check we would
-- otherwise have to write, and could get wrong, is the one GitHub already
-- performs.
--
-- No token column anywhere here, deliberately. Installation tokens are minted
-- on demand from the App's private key and held in memory for their hour.
-- A token at rest in a table is a token in every backup, every replica and
-- every `select *` a support script ever runs.

create table if not exists public.github_installations (
  id              uuid primary key default gen_random_uuid(),
  team_id         uuid not null references public.teams(id) on delete cascade,
  -- GitHub's own id for this installation. UNIQUE ACROSS ALL TEAMS, and that
  -- is a security property rather than tidiness: it is what stops a second
  -- team claiming an installation that already belongs to someone, and what
  -- makes routing a webhook by installation unambiguous. github_repos'
  -- (team_id, repo_full_name) uniqueness never had that property — two teams
  -- could both register the same repository, and resolve_team_for_repo picked
  -- whichever row came back first.
  installation_id bigint not null unique,
  -- The org or user the App was installed on, for showing a human which
  -- account they connected without a round trip to GitHub.
  account_login   text not null,
  created_at      timestamptz not null default now()
);

create index if not exists idx_github_installations_team
  on public.github_installations(team_id);

comment on table public.github_installations is
  'A GitHub App installation a team owns. Tokens are minted from these on '
  'demand (shared/github_app.py) and never stored.';

-- Which installation this repository is reached through. Nullable, because
-- rows predating the App exist; the insert policy below requires it for every
-- NEW row, so the null case ages out rather than being migrated with a guess
-- about which installation an old row should have belonged to.
alter table public.github_repos
  add column if not exists installation_id bigint
    references public.github_installations(installation_id) on delete cascade;

comment on column public.github_repos.installation_id is
  'The installation whose token fetches this repo. Null only for rows that '
  'predate the GitHub App; those cannot be cloned.';

alter table public.github_installations enable row level security;

-- ============================================================
-- Members read, leaders connect. Same shape github_repos already had.
-- ============================================================
drop policy if exists au_github_installations_select on public.github_installations;
create policy au_github_installations_select on public.github_installations
  for select to authenticated using (public.is_team_member(team_id));

-- INSERT is a leader action performed BY THE SERVER on the member's behalf,
-- after it has verified with GitHub that this person can actually administer
-- this installation. The server writes as the user rather than as admin, so
-- RLS still decides tenancy and the server only vouches for the GitHub half.
drop policy if exists au_github_installations_insert on public.github_installations;
create policy au_github_installations_insert on public.github_installations
  for insert to authenticated with check (public.is_team_leader(team_id));

drop policy if exists au_github_installations_delete on public.github_installations;
create policy au_github_installations_delete on public.github_installations
  for delete to authenticated using (public.is_team_leader(team_id));

-- No UPDATE policy at all. Repointing an installation at a different team is
-- not an edit, it is a re-connection: delete and install again. An UPDATE
-- policy here would be the one statement able to move a live credential
-- between tenants.

-- ============================================================
-- A repository may only be connected through an installation the SAME team
-- owns. Without this, a leader connects a repo naming another team's
-- installation id and borrows their access.
-- ============================================================
drop policy if exists au_github_repos_insert on public.github_repos;
create policy au_github_repos_insert on public.github_repos
  for insert to authenticated
  with check (
    public.is_team_leader(team_id)
    and exists (
      select 1 from public.github_installations i
      where i.installation_id = github_repos.installation_id
        and i.team_id = github_repos.team_id
    )
  );

-- The same rule on UPDATE. Connecting through a borrowed installation by
-- inserting a legitimate row and then editing it is the same attack with one
-- more step, and a policy that guards only INSERT invites exactly that.
drop policy if exists au_github_repos_update on public.github_repos;
create policy au_github_repos_update on public.github_repos
  for update to authenticated
  using (public.is_team_leader(team_id))
  with check (
    public.is_team_leader(team_id)
    and exists (
      select 1 from public.github_installations i
      where i.installation_id = github_repos.installation_id
        and i.team_id = github_repos.team_id
    )
  );

-- ============================================================
-- Who else needs to read an installation
-- ============================================================
-- The pipeline mints the token that clones and pushes (pipeline/repo_sync.py).
-- SELECT only: it reads which installation to mint for and never connects,
-- renames or removes one.
grant select on public.github_installations to comrade_pipeline;

drop policy if exists pl_github_installations on public.github_installations;
create policy pl_github_installations on public.github_installations
  for select to comrade_pipeline using (true);

-- The consent executor opens the pull request and mints a token to push with,
-- so it needs the same read, scoped to the team it is acting for — the same
-- shape as ex_github_repos_select in 20260902100000.
grant select on public.github_installations to comrade_executor;

drop policy if exists ex_github_installations_select on public.github_installations;
create policy ex_github_installations_select on public.github_installations
  for select to comrade_executor
  using (team_id = public.current_team());

-- comrade_agent is deliberately absent. The agent reads repository CONTENT
-- through the workspace tools; which installation backs it is infrastructure
-- it has no business naming, and a tool argument it cannot see is a tool
-- argument it cannot be talked into supplying.
