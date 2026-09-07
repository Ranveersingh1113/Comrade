-- What a thread has established, kept where a restart cannot lose it.
--
-- 🔴 The agent's memory of a thread was the last `agent_history_turns`
-- messages and nothing else. A constraint stated a hundred messages ago — "we
-- are not touching the vendored fork", "the customer is still on Postgres 14"
-- — was invisible to every later turn, so the agent would cheerfully propose
-- the thing the team had already ruled out, and the member had to say it
-- again. Nothing survived a worker restart either: whatever the turn had
-- worked out was in a prompt that no longer existed.
--
-- Pins are stored SEPARATELY from the summary on purpose. A rolling summary is
-- rewritten by a model every time it grows, and anything phrased as prose gets
-- paraphrased a little each round until it means something else. A constraint
-- the team stated is not a thing to paraphrase.
create table if not exists public.thread_working_state (
  thread_id        uuid primary key,
  team_id          uuid not null references public.teams(id) on delete cascade,

  --- the rolling summary and how far it has consumed
  summary          text not null default '',
  -- Keyset, not a timestamp: two messages can share one, and a summary cursor
  -- that skips a tied pair loses exactly the message somebody will ask about.
  summary_through      timestamptz,
  summary_through_id   uuid,

  --- things that must not be paraphrased away
  --- [{text, added_by, at, source_message_id}]
  pins             jsonb not null default '[]'::jsonb,
  --- [{text, asked_by, at}] — questions nobody has answered yet
  open_questions   jsonb not null default '[]'::jsonb,

  --- what the thread is working against
  plan_version     integer,
  workspace_revision text,
  --- {kind, detail} — waiting on a person, a run, a check
  pending          jsonb not null default '{}'::jsonb,

  updated_at       timestamptz not null default now(),
  foreign key (thread_id, team_id)
    references public.threads (id, team_id) on delete cascade
);

create index if not exists idx_thread_working_state_team
  on public.thread_working_state (team_id);

alter table public.thread_working_state enable row level security;

-- Members read and correct the state of a thread they can reach. Inspecting it
-- is the point: a summary a member cannot see is one they cannot correct, and
-- an agent working from a wrong summary is worse than one working from none.
grant select, insert, update on public.thread_working_state to authenticated;
drop policy if exists au_thread_working_state_select on public.thread_working_state;
create policy au_thread_working_state_select
  on public.thread_working_state for select to authenticated
  using (
    public.is_team_member(team_id)
    and public.can_access_thread(thread_id, (select auth.uid()))
  );
drop policy if exists au_thread_working_state_write on public.thread_working_state;
create policy au_thread_working_state_write
  on public.thread_working_state for update to authenticated
  using (
    public.is_team_member(team_id)
    and public.can_access_thread(thread_id, (select auth.uid()))
  )
  with check (
    public.is_team_member(team_id)
    and public.can_access_thread(thread_id, (select auth.uid()))
  );
drop policy if exists au_thread_working_state_insert on public.thread_working_state;
create policy au_thread_working_state_insert
  on public.thread_working_state for insert to authenticated
  with check (
    public.is_team_member(team_id)
    and public.can_access_thread(thread_id, (select auth.uid()))
  );

-- The agent reads it every turn and writes it when it compacts; the pipeline
-- writes it from the compaction job.
grant select, insert, update on public.thread_working_state to comrade_agent;
grant select, insert, update on public.thread_working_state to comrade_pipeline;
drop policy if exists ex_thread_working_state on public.thread_working_state;
create policy ex_thread_working_state on public.thread_working_state
  for all to comrade_agent, comrade_pipeline
  using (team_id = public.current_team()) with check (team_id = public.current_team());
