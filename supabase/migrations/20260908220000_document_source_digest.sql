-- Which bytes were actually compiled, and whether that was checked.
--
-- 🔴 THE DEFECT (fix.md F22). `content_sha256` was recorded into the job
-- payload at enqueue and never compared with anything. T21 moved the document
-- bytes out of that payload and left a REFERENCE in their place, which is
-- right — but a reference is only as good as the check that it still points at
-- what was meant. Replacing the object at the same storage path between
-- enqueue and fetch silently compiled different input into the team wiki,
-- carrying the citation of the file somebody had actually reviewed.
--
-- Two columns, because they answer different questions and conflating them
-- would be the same mistake again:
--
--   `content_sha256`  — what was compiled. Always recorded.
--   `content_verified` — whether it was compared against what the job asked
--                        for. False for a job enqueued before the check
--                        existed, and false is not the same as unknown being
--                        quietly presented as fine.
alter table public.documents
  add column if not exists content_sha256 text,
  add column if not exists content_verified boolean not null default false;

grant select (content_sha256, content_verified) on public.documents to authenticated;
grant select (content_sha256, content_verified),
      update (content_sha256, content_verified)
  on public.documents to comrade_pipeline;
