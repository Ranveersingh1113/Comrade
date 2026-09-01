-- Publishing work from the private thread into the room (findings §13.4).
--
-- A member working with Comrade privately gets to put that work in front of
-- the team. Owner decision 2026-08-17 chose the shape: THE MEMBER'S OWN
-- MESSAGE, carrying a marker.
--
--   attribution   sender_kind='user', sender_id = auth.uid() -- the member
--                 owns it and stands behind it
--   provenance    this column, rendered as "drafted with Comrade"
--   transport     a plain insert from the browser
--   consent       none -- a human composing and sending their own message is
--                 not a gated action
--
-- WHY NO ENDPOINT AND NO POLICY CHANGE
-- --------------------------------------
-- au_messages_insert (20260612095500_rls.sql:128) already requires exactly
-- `sender_kind = 'user' and sender_id = auth.uid()`, and RLS gates ROWS, not
-- columns -- so a member may set this on their own insert with nothing added.
-- Verified against the policy text again here rather than taken on trust,
-- because "no change needed" is the kind of claim that is cheap to assert and
-- expensive to be wrong about.

alter table public.messages
  add column if not exists ai_assisted boolean not null default false;

-- An AI message claiming to be AI-assisted is nonsense: the marker means "a
-- person wrote this WITH help", and on Comrade's own output it would either be
-- redundant or a lie about who is speaking. §13.4 raised it as worth
-- considering; it is worth doing, because the column is otherwise free for
-- anything writing a message to set.
alter table public.messages
  drop constraint if exists messages_ai_assisted_is_human;
alter table public.messages
  add constraint messages_ai_assisted_is_human
  check (not ai_assisted or sender_kind = 'user');

comment on column public.messages.ai_assisted is
  'The member drafted this with Comrade in their private thread and published '
  'it themselves (findings §13.4). Attribution stays with the member; this is '
  'provenance, not authorship.';
