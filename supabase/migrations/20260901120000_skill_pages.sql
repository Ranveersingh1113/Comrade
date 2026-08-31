-- A wiki page can hold a procedure, not just facts.
--
-- findings §6.3-3 states the gap plainly: "facts only -- no skills, no
-- procedural memory". §24.2 is what promotes it from optional layering: a team
-- with no manager loses the handoff, and the substitute is a shared standard --
-- "a playbook everyone knows cold before they sit down". A standard IS
-- procedural memory, and it is the artifact that replaces what a manager used
-- to carry in their head.
--
-- The distinction is not cosmetic. "The demo is on 14 March" is a fact about
-- the project; "run the migration before deploying" is how this team does
-- something. An agent answering "what should I do next" needs to tell them
-- apart, and until now the wiki rendered both as identical bullets.

alter table public.memory_pages
  add column if not exists kind text not null default 'fact';

-- Constrained, not free text: a typo would create a third kind that renders
-- nowhere and that nothing would ever notice.
alter table public.memory_pages
  drop constraint if exists memory_pages_kind_check;
alter table public.memory_pages
  add constraint memory_pages_kind_check check (kind in ('fact','skill'));

comment on column public.memory_pages.kind is
  'fact = things that are true about the project; skill = how the team does '
  'something (findings §24.2 procedural memory). Defaults to fact: every page '
  'predating this column is a fact page, and guessing otherwise would relabel '
  'a team''s whole wiki on a migration.';

-- ============================================================
-- What is NOT here: `scope`
-- ============================================================
-- The Phase 4 plan paired this with a `scope` column (team / personal /
-- restricted, per §4.5). Reading §4.5 closely says not to, yet.
--
-- Its advice is CONDITIONAL: *if* you build private memory, make it a scope on
-- these same pages rather than a parallel store. That is guidance about shape,
-- not a requirement to build private memory now -- and §6.2-1 parked the
-- persona layer deliberately.
--
-- Today every page is team-visible by construction: private threads never
-- reach memory (§6.0), so there is no second scope with anything in it. The
-- column would always read 'team'. And `restricted` would be worse than
-- useless: this schema has no notion of "some members", so the value would
-- have no enforcement behind it while looking, in a policy or a code review,
-- exactly as though it did.
--
-- §4.5 is honoured by NOT pre-empting it. When personal memory earns its way
-- in, it arrives as a scope column on these pages -- not a parallel store --
-- and whoever adds it will have to say what enforces each value. There is a
-- test pinning this absence so the next reader finds the argument rather than
-- an apparent oversight.
