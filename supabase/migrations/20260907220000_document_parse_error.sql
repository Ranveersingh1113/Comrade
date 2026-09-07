-- Why a document failed to parse, where the member can see it.
--
-- 🔴 `status='failed'` and nothing else. A member whose upload failed was told
-- only that it had, with no way to tell "this is a scanned image" from "this
-- file is too big" from "we cannot read legacy .doc" — three problems with
-- three different answers, and the same blank wall in front of all of them.
alter table public.documents
  add column if not exists parse_error text;

comment on column public.documents.parse_error is
  'Why the last parse failed, in words a member can act on. Cleared when a '
  'parse succeeds.';
