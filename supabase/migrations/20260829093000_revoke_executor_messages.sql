-- findings §13.3-1. team_propose_group_message was the only consent tool whose
-- executor wrote to `messages`. With it removed, _EXECUTORS holds only
-- task_create, whose executor writes `tasks` and nothing else — so
-- `grant insert on public.messages to comrade_executor`
-- (20260612120000_action_consent.sql:45) is dead privilege.
--
-- Least privilege is the whole point of the role split; a granted-but-unused
-- write is exactly the thing that silently becomes reachable again later.

revoke insert on public.messages from comrade_executor;
drop policy if exists ex_messages_insert on public.messages;
