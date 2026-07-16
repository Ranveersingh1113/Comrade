-- Drop vector-retrieval storage (owner decision 2026-07-15: PromptQL-style
-- wiki-page memory replaces retrieval-style memory; consolidation reads the
-- whole active fact set — pilot corpora fit in the model's context window).
-- The `vector` extension stays installed (Supabase-bundled; dropping columns
-- removes the dependent HNSW index automatically, listed explicitly anyway).

drop index if exists public.idx_memory_versions_active_embedding;

alter table public.memory_versions drop column if exists embedding;
alter table public.memory_versions drop column if exists embedding_model;
alter table public.memory_versions drop column if exists embedding_dim;
