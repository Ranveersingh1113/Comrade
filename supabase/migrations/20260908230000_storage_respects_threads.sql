-- Storage authorizes the same way the document does.
--
-- 🔴 THE DEFECT (fix.md F18). `st_documents_select` authorized by the FIRST
-- PATH SEGMENT: `is_team_member(<team id from the path>)`. T24 gave documents a
-- thread and a purpose, so an attachment dropped in a restricted conversation
-- is denied to a non-participant by `au_documents_select` — and the bytes were
-- readable by any member of the team straight out of Storage, through
-- `createSignedUrl` or a direct object read, without touching the documents
-- API or the frontend at all.
--
-- Scoping metadata while leaving the bytes on a team-wide rule is not a
-- privacy boundary; it is a detour sign.
--
-- This is only expressible because of 20260908180000: one object belongs to
-- exactly one document row and cannot be re-pointed, so "the document that
-- owns this object" is a question with a single answer.
drop policy if exists st_documents_select on storage.objects;
create policy st_documents_select on storage.objects for select to authenticated
  using (
    bucket_id = 'documents'
    and exists (
      select 1 from public.documents d
      where d.storage_path = storage.objects.name
        and public.is_team_member(d.team_id)
        -- The same clause `au_documents_select` uses. Two policies that mean
        -- to say the same thing should say it the same way.
        and (d.thread_id is null
             or public.can_access_thread(d.thread_id, (select auth.uid())))
        -- A withdrawn document's bytes are withdrawn too. Deletion here is
        -- soft by design, and "soft" should not mean the file stayed readable.
        and d.deleted_at is null
    )
  );

-- INSERT stays on the team rule, deliberately: the frontend uploads the object
-- and THEN inserts the row, so at write time there is no document to consult.
-- An object whose row never arrives is unreadable by everyone under the policy
-- above, which is the right way for that race to fail.
