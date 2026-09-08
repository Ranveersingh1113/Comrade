-- A stored object belongs to exactly one document, and that binding is fixed.
--
-- 🔴 THE DEFECT (fix.md F20). `documents.storage_path` is written by MEMBERS —
-- the frontend inserts the row under RLS — and `shared/storage.py:download_document`
-- fetches whatever path it is handed using the Supabase SERVICE SECRET, with no
-- check that the object belongs to the document, the team, or the thread.
--
-- So a member could create a document row in their own team pointing at another
-- team's object path, or at a restricted same-team attachment they are not a
-- participant of, and the pipeline would read it with full privilege and
-- compile its text into a wiki they can see. Knowing the path is a
-- prerequisite, not authority — and same-team paths are the easy case, because
-- the prefix is the team id the member already knows.
--
-- Two structural guards, because the read boundary alone cannot tell an alias
-- from a legitimate reference:
--
--   1. UNIQUE. One object, one document row. A second row pointing at an
--      object that already belongs to a document is refused outright, which is
--      exactly the aliasing move above — including the restricted same-team
--      case, where a team-prefix check would pass.
--
--   2. IMMUTABLE. A row cannot be re-pointed after insert. Without this, a
--      member creates a document with their own object, waits for it to be
--      accepted, and then edits `storage_path` to the one they want read.
--
-- 🔴 THIS COMMENT USED TO CLAIM that `shared/storage.py` additionally refused
-- a path outside the reading team's own prefix. IT DID NOT — `download_document`
-- took a path and nothing else, and fetched it with the service secret. The
-- claim described code that was never written, which left the two guards above
-- looking complete when they only cover objects that ALREADY have a document
-- row. An object in the upload-before-metadata window, or one whose row was
-- hard-deleted, was still claimable.
--
-- A prefix check would not have been sufficient either: a restricted same-team
-- attachment sits under the reader's own prefix. The real check is object
-- OWNERSHIP, added in 20260909100000_object_authenticity.sql and enforced in
-- shared/storage.py:assert_object_is_authentic.
--
-- Only the comment is corrected here; the migration's statements are unchanged.

-- Existing rows first: a duplicate path today would make the index creation
-- fail during a release, and finding that out at deploy time is worse than
-- finding it out here.
do $$
declare
  n integer;
begin
  select count(*) into n from (
    select storage_path from public.documents
     where storage_path is not null
     group by storage_path having count(*) > 1
  ) dupes;
  if n > 0 then
    raise exception
      'refusing to bind objects to documents: % storage paths are already'
      ' referenced by more than one document row. Resolve those before'
      ' applying this migration.', n;
  end if;
end $$;

create unique index if not exists idx_documents_storage_path
  on public.documents (storage_path)
  where storage_path is not null;

create or replace function public.trg_document_object_is_fixed()
returns trigger language plpgsql as $$
begin
  -- Null -> a path is the ordinary case: the row is created, then the upload
  -- completes and the path is recorded. Path -> different path is not.
  if old.storage_path is not null
     and new.storage_path is distinct from old.storage_path then
    raise exception 'a document''s stored object cannot be changed';
  end if;
  return new;
end $$;

drop trigger if exists document_object_is_fixed on public.documents;
create trigger document_object_is_fixed
  before update on public.documents
  for each row execute function public.trg_document_object_is_fixed();
