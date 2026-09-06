-- Single-use launch grants for preview origins.
--
-- 🔴 WHY THE ORIGIN CHANGED. Previews were served from Comrade's own hostname
-- under /previews/<id>/. That puts a development server — written by a model,
-- running a team's unreviewed code — on the SAME BROWSER ORIGIN as the
-- application, so its JavaScript can read the member's Supabase session out of
-- localStorage. Stripping headers at the proxy does nothing about that: the
-- boundary a browser enforces is the origin, and there was only one.
--
-- Each process now answers on its own hostname. The browser cannot carry a
-- Comrade cookie or a Supabase token there, because it is a different site.
--
-- Which leaves one problem: a fresh origin has no credential at all, and the
-- token cannot live in the URL of every asset request. So the app origin mints
-- a single-use GRANT, the browser is sent to the preview origin once carrying
-- it, and that origin exchanges it for a host-scoped HttpOnly cookie. This
-- table is what makes "single-use" true rather than aspirational — a grant
-- replayed from a browser history entry or a screenshot must not work twice.
create table public.preview_grants (
  jti          uuid primary key,
  process_id   uuid not null references public.sandbox_processes(id) on delete cascade,
  team_id      uuid not null references public.teams(id) on delete cascade,
  user_id      uuid not null references public.profiles(id) on delete cascade,
  -- The exact hostname this grant may be redeemed on. A grant redeemed against
  -- another process's origin would hand one thread's member a session for
  -- another thread's server.
  host         text not null,
  expires_at   timestamptz not null,
  consumed_at  timestamptz,
  created_at   timestamptz not null default now()
);

create index idx_preview_grants_expiry on public.preview_grants (expires_at)
  where consumed_at is null;

revoke all on public.preview_grants from authenticated;
alter table public.preview_grants enable row level security;

-- Nobody reads these from a browser. The app origin writes one, the preview
-- origin consumes it, and both are server-side.
grant select, insert, update on public.preview_grants to comrade_agent;
create policy ag_preview_grants on public.preview_grants for all to comrade_agent
  using (team_id = public.current_team())
  with check (team_id = public.current_team());

-- Expiry sweeping is cross-team maintenance; it needs no identity columns.
grant select (jti, expires_at, consumed_at), delete on public.preview_grants
  to comrade_control;
create policy ctl_preview_grants on public.preview_grants
  for all to comrade_control using (true) with check (true);
