-- Is this object actually the one this document uploaded?
--
-- 🔴 (fix.md F20, reopened.) The first repair added a UNIQUE index and an
-- immutability trigger, and both are real — together they stop a second
-- document row claiming an object that ALREADY belongs to one. They say
-- nothing about an object that belongs to none:
--
--   * the upload-before-metadata interval, where the object lands and the
--     insert that would own it never arrives;
--   * an object whose document row was hard-deleted.
--
-- In that window a member can file a row in their OWN team pointing at
-- another team's object, or at a restricted same-team attachment, and
-- `shared/storage.py:download_document` fetches it with the service secret,
-- which is not subject to RLS at all. Knowing a path is a prerequisite, not
-- authority.
--
-- The migration that added those guards also claimed `shared/storage.py`
-- refused paths outside the reading team's prefix. It did not — the function
-- took a path and nothing else. That comment is corrected below, and a prefix
-- check would not have been sufficient anyway: a restricted attachment sits
-- under the reader's own team prefix, which is the second test in
-- tests/test_object_authenticity.py.
--
-- WHAT AUTHENTICITY IS. Every upload into this bucket arrives through the
-- member's own browser session (frontend/src/screens/Documents.tsx and
-- Setup.tsx), so Storage records `owner_id` = that member and the row the
-- member then inserts carries `uploader_id` = the same member. A row claiming
-- an object it did not upload is the entire attack, and it is one comparison.
--
-- `owner_id`, not `owner`: measured on this stack, `owner` is NULL on objects
-- uploaded through the JS client while `owner_id` carries the uid.
--
-- SECURITY DEFINER because it has to see `storage.objects`, which no Comrade
-- role holds and none should. It answers one boolean about one document and
-- discloses nothing else.
create or replace function public.document_object_is_authentic(
  p_document_id uuid,
  p_path text
) returns boolean
language sql stable security definer set search_path = '' as $$
  select exists (
    select 1
      from public.documents d
      join storage.objects o
        on o.bucket_id = 'documents'
       and o.name = d.storage_path
     where d.id = p_document_id
       -- The path asked for is the path the row records. A read is FOR one
       -- document; another document's object is not its content.
       and d.storage_path = p_path
       -- Under the owning team's own prefix. Cheap, and it fails the
       -- cross-team case before ownership is consulted.
       and split_part(d.storage_path, '/', 1) = d.team_id::text
       -- And uploaded by the member the row claims uploaded it. This is the
       -- part a prefix check cannot do: a restricted same-team attachment is
       -- under the right prefix and belongs to somebody else.
       and o.owner_id = d.uploader_id::text
  );
$$;

revoke all on function public.document_object_is_authentic(uuid, text) from public;
-- Only the role that actually performs the privileged read.
grant execute on function public.document_object_is_authentic(uuid, text)
  to comrade_pipeline;
