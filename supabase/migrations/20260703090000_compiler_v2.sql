-- Compiler v2: INVALIDATE verb + embedding provenance.

-- 1. Allow 'invalidated' versions (tombstones: is_active=false, cite the
--    retracting source; the entry keeps its history but has no active fact).
alter table public.memory_versions
  drop constraint memory_versions_change_type_check;
alter table public.memory_versions
  add constraint memory_versions_change_type_check
  check (change_type in ('added','revised','reverted','invalidated'));

-- 2. Per-row embedding provenance. gemini-embedding-2's vector space is
--    incompatible with -001, so a future upgrade must be a phased per-row
--    backfill — these columns make that possible without a flag day.
alter table public.memory_versions add column embedding_model text;
alter table public.memory_versions add column embedding_dim  integer;

update public.memory_versions
  set embedding_model = 'gemini-embedding-001', embedding_dim = 1536
  where embedding is not null;

-- 3. Correct the stale inline comment from init.sql (text-embedding-3-small
--    was never shipped; the live path is Gemini).
comment on column public.memory_versions.embedding is
  'gemini-embedding-001, MRL-truncated to 1536 dims + L2-normalised. Model/dim recorded per row in embedding_model/embedding_dim.';
