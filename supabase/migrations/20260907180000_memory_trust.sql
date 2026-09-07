-- How much support a fact in the wiki actually has.
--
-- 🔴 Every version written was a published fact. An excerpt went into
-- `memory_citations` exactly as the model produced it, with nothing checking
-- that it appears in the source — so a generated quote became evidence by
-- itself, and a citation is the one thing a member looks at to decide whether
-- to believe a fact.
--
-- The vocabulary is deliberately modest. `observed` means the excerpt was
-- found in the source this team can read; it does NOT mean the fact follows
-- from the source, because substring matching cannot establish entailment and
-- a column that claimed it could would be worse than no column.
alter table public.memory_versions
  add column if not exists trust text not null default 'observed';

alter table public.memory_versions drop constraint if exists memory_versions_trust_check;
alter table public.memory_versions add constraint memory_versions_trust_check check (
  trust in ('proposed', 'observed', 'confirmed', 'verified', 'superseded')
) not valid;
alter table public.memory_versions validate constraint memory_versions_trust_check;

comment on column public.memory_versions.trust is
  'proposed: no support could be verified — quarantined, never active. '
  'observed: the excerpt was found in a source this team can read. '
  'confirmed/verified: a person vouched for it. superseded: replaced. '
  'None of these assert that the fact FOLLOWS from the source; substring '
  'matching cannot establish entailment.';
