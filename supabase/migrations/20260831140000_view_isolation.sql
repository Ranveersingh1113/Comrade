-- 🔴 Views are not covered by row-level security. These two were a hole
-- straight through the model. Found by the read-path audit, 2026-08-31.
--
-- RLS protects TABLES. A view has no policies of its own, and unless told
-- otherwise it executes with the privileges of its OWNER — `security_invoker`
-- defaults to off. So a view over RLS-protected tables, owned by a superuser
-- and granted to `authenticated`, bypasses every policy underneath it.
--
-- contribution_v is exactly that, and the product reads it on every group-room
-- render. Measured before this migration: member B1, who belongs to one team,
-- selected rows for THREE teams — user ids, task counts, github event counts,
-- message counts and activity recency for every team in the database. Not
-- message content, but the roster and work-shape of every team on the
-- instance, which is precisely what the rest of this schema exists to isolate.
--
-- document_opens_summary is the same shape and was created with an explicit
-- `security_invoker=off`.
--
-- Note this is the read-side twin of the write-side default already recorded
-- in docs/architecture.md: Supabase grants anon and authenticated full CRUD on
-- every new relation. A TABLE survives that because RLS still runs. A VIEW
-- does not, so on a view the GRANT is the whole boundary — and PostgREST
-- serves unauthenticated requests as `anon`.

alter view public.contribution_v         set (security_invoker = on);
alter view public.document_opens_summary set (security_invoker = on);

-- With the views now running as the caller, `authenticated` is gated by the
-- underlying policies and needs nothing else. `anon` is never a legitimate
-- reader of either, and on a view its grant is not backstopped by anything.
revoke all on public.contribution_v         from anon;
revoke all on public.document_opens_summary from anon;

-- Neither view is writable and nothing should think it is.
revoke insert, update, delete, truncate, references
  on public.contribution_v         from authenticated;
revoke insert, update, delete, truncate, references
  on public.document_opens_summary from authenticated;
