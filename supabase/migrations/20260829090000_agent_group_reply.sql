-- ============================================================
-- MIGRATION POLICY (findings doc §8, §16.5) — in force from here onward
-- ------------------------------------------------------------
--   * Any index on an EXISTING table uses CREATE INDEX CONCURRENTLY, in its
--     own migration file with no surrounding transaction.
--   * Any check constraint on an EXISTING table is added NOT VALID first,
--     then VALIDATE CONSTRAINT as a separate statement.
-- ============================================================

-- Fix: the agent's reply to an explicit @comrade invocation is one of the two
-- group-visible AI writes kept by §13.1, but ag_messages_insert
-- (20260612120000_action_consent.sql:33) required thread_type='private'.
-- server/app.py:_persist_ai_reply inserts thread_type='group' on a group turn
-- under Role.AGENT, so that path failed at RLS with
--   "new row violates row-level security policy for table messages".
--
-- Nothing caught it: tests/test_server.py and tests/test_server_stream.py both
-- monkeypatch _persist_ai_reply, and tests/test_runtime_live.py calls run_turn
-- directly rather than going through the endpoint. tests/test_rls_agent_reply.py
-- is the missing coverage.
--
-- The invariant this policy exists to protect is sender_kind='ai': the agent
-- may never write a message attributed to a human. Thread type was never the
-- guard, and restricting it broke the product's headline interaction.
--
-- This does NOT re-open agent-initiated group posting. That capability is
-- team_propose_group_message, removed separately by §13 — the agent has no
-- tool that reaches this policy on its own initiative. What remains is the
-- server persisting a reply to a question a human asked in the room.
--
-- The agent still holds NO update on messages (20260612120000:27), so it can
-- write an AI message and never edit one.

drop policy if exists ag_messages_insert on public.messages;
create policy ag_messages_insert on public.messages for insert to comrade_agent
  with check (team_id = public.current_team() and sender_kind = 'ai');
