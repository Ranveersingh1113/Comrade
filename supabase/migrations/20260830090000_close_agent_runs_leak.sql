-- 🔴 Second private-thread leak, found by the Phase 0 whole-branch review.
--
-- findings §2.1 closed the leak on `messages`: the agent role held team-scoped,
-- not thread-scoped, SELECT. It missed the SECOND COPY of the same content.
--
-- `agent_runs.input_summary` stores the member's prompt verbatim
-- (agent/runtime.py passes user_text[:200]) and `agent_runs.steps` stores every
-- tool call and tool result. `au_agent_runs_select` was
-- `using (public.is_team_member(team_id))`, so ANY teammate could read ANY
-- member's private-thread turn directly from the browser.
--
-- This was worse than §2.1, which was latent because no tool exposed messages
-- to the model. This one was live and browser-reachable the whole time.
--
-- Proved on the live database before writing this migration: member A2 issued
--   select input_summary, steps from public.agent_runs where team_id = <A's team>
-- and received A1's private prompt verbatim plus A1's tool results.
--
-- The fix DELETES the grant rather than narrowing it, because nothing
-- member-facing reads this table — `grep -rn agent_runs frontend/` is empty.
-- The only reader running as `authenticated` was _check_turn_budget, a
-- server-side rate limit, which moves to the agent role: team-scoped by
-- current_team(), and it returns a count to the server, never rows to a member.
--
-- If a "your run history" surface is ever built, THAT is the moment to add a
-- requester column and an own-rows-only policy. Not before — a narrowed policy
-- on a table nobody reads is a leak waiting for its second chance.

drop policy if exists au_agent_runs_select on public.agent_runs;
revoke select on public.agent_runs from authenticated;

-- ---------------------------------------------------------------------------
-- Same shape, sibling role: comrade_executor's SELECT on messages.
--
-- Team-scoped, thread-scoped by nothing — byte-identical in shape to §2.1.
-- It was load-bearing only for _exec_post_group_message's `returning id`, and
-- §13 removed that executor. 20260829093000 revoked the executor's INSERT for
-- exactly this reasoning and left the SELECT behind.
--
-- Least privilege is the whole point of the role split; a granted-but-unused
-- read is exactly the thing that silently becomes reachable again later.

revoke select on public.messages from comrade_executor;
drop policy if exists ex_messages_select on public.messages;
