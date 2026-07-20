-- Invite-by-email support.
--
-- Two gaps blocked inviting someone who has never logged in:
--   1. nothing created a profiles row for a new auth user (the seed data and
--      the signup path both inserted it manually), and memberships FK-requires
--      one, so the leader could not add the membership;
--   2. no role could resolve "does this email already have an account".

-- Auto-create a profile for every new auth user. Display name falls back to
-- the email local part; the member can edit it later (profiles: edit self).
create or replace function public.handle_new_user()
returns trigger language plpgsql security definer set search_path = '' as $$
begin
  insert into public.profiles (id, display_name)
  values (
    new.id,
    coalesce(
      new.raw_user_meta_data ->> 'display_name',
      split_part(new.email, '@', 1),
      'member'
    )
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

create trigger trg_auth_user_profile
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- Backfill: any auth user created before this trigger (e.g. via GoTrue
-- invites) gets their profile now.
insert into public.profiles (id, display_name)
select u.id, coalesce(split_part(u.email, '@', 1), 'member')
from auth.users u
left join public.profiles p on p.id = u.id
where p.id is null
on conflict (id) do nothing;

-- Email -> user id, for the invite endpoint's "already registered" path.
-- Definer (auth schema is closed to worker roles), granted ONLY to the agent
-- role — not to authenticated, so members cannot enumerate accounts.
create or replace function public.user_id_by_email(p_email text)
returns uuid language sql security definer set search_path = '' as $$
  select id from auth.users where lower(email) = lower(p_email) limit 1;
$$;

revoke all on function public.user_id_by_email(text) from public, anon, authenticated;
grant execute on function public.user_id_by_email(text) to comrade_agent;
