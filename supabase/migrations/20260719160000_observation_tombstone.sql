-- Narrow capability for the suppress-observation flow.
--
-- The agent role is deliberately propose-only: it may INSERT messages (its own
-- replies) but its UPDATE on messages was revoked with the consent split, and
-- that stance is correct — suppression must not become a general message-edit
-- power. This definer function grants exactly one thing: marking an AI
-- message deleted-for-everyone (the deletion-leaves-a-trace invariant), and
-- nothing else. Members and anon cannot execute it at all.

create or replace function public.tombstone_ai_message(
  p_message_id uuid, p_team_id uuid
) returns boolean language plpgsql security definer set search_path = '' as $$
declare
  hit boolean;
begin
  update public.messages
     set deleted_scope = 'everyone', deleted_at = now()
   where id = p_message_id and team_id = p_team_id
     and sender_kind = 'ai' and deleted_at is null
  returning true into hit;
  return coalesce(hit, false);
end;
$$;

revoke all on function public.tombstone_ai_message(uuid, uuid)
  from public, anon, authenticated;
grant execute on function public.tombstone_ai_message(uuid, uuid) to comrade_agent;
