-- findings §2.1 + §4.1. The agent borrows the requester's permissions for
-- reads; it keeps its own identity only for writes.
--
-- §2.1 (🔴): ag_messages_select was team-scoped, not thread-scoped, and
-- comrade_agent held select on public.messages. Nothing exposed messages to
-- the model yet, so it was latent — it goes live the moment messages_search
-- exists (Phase 2). Fixing it by adding a thread_owner_id clause would be the
-- wrong shape: the agent should not have a private read surface at all.
--
-- Independently reached by two external systems: PromptQL ("if you can't see
-- it, the agent can't see it for you", §6.1) and Claude Code, which has no
-- agent identity at all — every decision is made against the user's rules
-- (§9.1).
--
-- REMAINING agent reads, and why each survives:
--   nudge_log   — the 24h cooldown lookup in shared/nudge.py, a write-path read
--   agent_runs  — shared/agent_runs.py:get_run, the agent's own audit trail
--   change_log  — the audit trigger writes it under this role
-- Everything else is now read through user_session().
--
-- NOTE for the two INSERT paths below: `INSERT ... RETURNING` requires SELECT
-- privilege (and a SELECT policy) on the table, so revoking select breaks any
-- writer that used it. server/app.py:_persist_ai_reply and
-- shared/consent.py:propose_action now generate the row id in Python instead.
-- That is the correct direction: the write keeps working and the read surface
-- stays deleted.

revoke select on
  public.profiles, public.teams, public.memberships, public.messages,
  public.documents, public.document_opens,
  public.memory_compilations, public.memory_entries, public.memory_versions,
  public.memory_citations, public.memory_reverts,
  public.tasks, public.milestones, public.github_repos, public.github_activity,
  public.consent_queue
from comrade_agent;

revoke select on public.memory_pages from comrade_agent;
revoke select on public.observation_suppressions from comrade_agent;

-- The policies are now unreachable (no grant to gate). Drop them so the next
-- reader is not misled into thinking the agent still has a read surface.
drop policy if exists ag_profiles              on public.profiles;
drop policy if exists ag_teams                 on public.teams;
drop policy if exists ag_memberships           on public.memberships;
drop policy if exists ag_messages_select       on public.messages;
drop policy if exists ag_documents             on public.documents;
drop policy if exists ag_document_opens        on public.document_opens;
drop policy if exists ag_memory_compilations   on public.memory_compilations;
drop policy if exists ag_memory_entries        on public.memory_entries;
drop policy if exists ag_memory_versions       on public.memory_versions;
drop policy if exists ag_memory_citations      on public.memory_citations;
drop policy if exists ag_memory_reverts        on public.memory_reverts;
drop policy if exists ag_memory_pages          on public.memory_pages;
drop policy if exists ag_tasks                 on public.tasks;
drop policy if exists ag_milestones            on public.milestones;
drop policy if exists ag_github_repos          on public.github_repos;
drop policy if exists ag_github_activity       on public.github_activity;
drop policy if exists ag_obs_suppressions      on public.observation_suppressions;

-- consent_queue: the agent still INSERTS proposals, so its policy stays but
-- narrows to insert only. It never needed to read the queue back.
drop policy if exists ag_consent_queue on public.consent_queue;
create policy ag_consent_queue_insert on public.consent_queue
  for insert to comrade_agent
  with check (team_id = public.current_team());
