-- findings §3.1: `documents.parsed_text` is missing, so a document's extracted
-- text lives only in a transient job payload (pipeline/compiler.py) and is gone
-- once the job finishes. `document_read` — the tool that lets the agent open a
-- document the team uploaded — would have nothing to open.
--
-- Written by comrade_pipeline only, which already holds `update` on documents
-- (20260612095500_rls.sql). The agent never writes here, and since §4.1 it does
-- not read this table under its own role either: document_read runs as the
-- requesting member, so au_documents_select is what gates it.
--
-- Untrusted content: this is verbatim source text and must be spotlighted
-- before it reaches any LLM, exactly as the compile path already does.

alter table public.documents
  add column if not exists parsed_text text;

comment on column public.documents.parsed_text is
  'Extracted document text, written by comrade_pipeline at parse time. Source '
  'for document_read. Untrusted content — spotlight() before any LLM call.';
