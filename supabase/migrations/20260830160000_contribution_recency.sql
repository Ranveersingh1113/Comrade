-- A recency signal, so the `idle` nudge finally has a data source.
--
-- findings §3.1 lists "Recency signal in contribution_v" as missing, and notes
-- the consequence: the `idle` nudge type exists — templates, cooldown,
-- suppression machinery, all built — "with no data source to trigger it".
-- Nothing in the product could ever fire it.
--
-- Governance (platform-findings memo, ruling 6) restricts person-to-person
-- comparison. These are RECENCY facts per member, answering "who might be
-- stuck", not volume rankings answering "who is doing least". The view already
-- carries counts for the contribution screen; these two columns deliberately
-- carry timestamps instead, and member_activity does not sort by anything.
--
-- last_message_at is GROUP messages only. A private thread with the AI is not
-- evidence of team participation — that is the whole point of the signal, and
-- counting it would mean a member who talks only to the bot never looks idle.
--
-- Scalar subqueries in the existing style: the view already uses four of them
-- to avoid join fan-out across its signal sources.

create or replace view public.contribution_v as
select
  m.team_id,
  m.user_id,
  (select count(*) from public.tasks t
     where t.team_id = m.team_id and t.assignee_id = m.user_id
       and t.status = 'done')                                   as tasks_done,
  (select count(*) from public.tasks t
     where t.team_id = m.team_id and t.assignee_id = m.user_id
       and t.status in ('confirmed','in_progress','done'))      as tasks_active,
  (select count(*) from public.github_activity g
     where g.team_id = m.team_id and g.author_user_id = m.user_id) as github_events,
  (select count(*) from public.messages msg
     where msg.team_id = m.team_id and msg.thread_type = 'group'
       and msg.sender_kind = 'user' and msg.sender_id = m.user_id
       and msg.deleted_scope is null)                            as group_messages,
  (select max(msg.created_at) from public.messages msg
     where msg.team_id = m.team_id and msg.thread_type = 'group'
       and msg.sender_kind = 'user' and msg.sender_id = m.user_id
       and msg.deleted_scope is null)                            as last_message_at,
  (select max(t.updated_at) from public.tasks t
     where t.team_id = m.team_id and t.assignee_id = m.user_id)  as last_task_at
from public.memberships m
where m.status = 'active';

grant select on public.contribution_v to authenticated;
