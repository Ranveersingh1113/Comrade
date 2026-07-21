-- The documents bucket and its RLS.
--
-- Storage was the one surface with no policies at all: the frontend uploaded
-- to a bucket that did not exist, and hand-creating that bucket without
-- policies would have made every team's documents readable by any
-- authenticated user of the project.
--
-- Upload paths are `{team_id}/{uuid}-{filename}` (frontend Documents.tsx), so
-- the first path segment is the tenant key. Reads go through signed URLs
-- (createSignedUrl), so the bucket stays private.

insert into storage.buckets (id, name, public)
values ('documents', 'documents', false)
on conflict (id) do nothing;

-- SELECT + INSERT only: the app never updates or deletes objects. Document
-- deletion is soft (documents.deleted_at) and leaves the file in place.
--
-- ponytail: the ::uuid cast raises rather than denies on a malformed first
-- segment. Nothing can store such a path (the same cast guards INSERT), so
-- only a service_role write could create one. Swap to a text comparison
-- against the caller's team ids if that ever happens.
create policy st_documents_select on storage.objects for select to authenticated
  using (
    bucket_id = 'documents'
    and public.is_team_member(((storage.foldername(name))[1])::uuid)
  );

create policy st_documents_insert on storage.objects for insert to authenticated
  with check (
    bucket_id = 'documents'
    and public.is_team_member(((storage.foldername(name))[1])::uuid)
  );
