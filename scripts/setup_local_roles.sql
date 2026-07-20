-- LOCAL DEV ONLY: make the worker group roles connectable with a password.
-- Production uses per-environment LOGIN roles / secrets — do NOT run this there.
--
-- Run (passwords come from your .env, never committed):
--   docker exec -i supabase_db_Comrade psql -U postgres -d postgres \
--     -v agent_pwd=<pwd> -v executor_pwd=<pwd> -v pipeline_pwd=<pwd> \
--     -f - < scripts/setup_local_roles.sql
--
-- :'var' quotes the value as a SQL string literal.

alter role comrade_agent    with login password :'agent_pwd';
alter role comrade_executor with login password :'executor_pwd';
alter role comrade_pipeline with login password :'pipeline_pwd';

-- PostgREST-style authenticator for user_session(): may become `authenticated`
-- and nothing else. noinherit = its own privileges stay empty until SET ROLE.
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'comrade_authenticator') then
    create role comrade_authenticator noinherit;
  end if;
end $$;
alter role comrade_authenticator with login noinherit password :'authenticator_pwd';
grant authenticated to comrade_authenticator;
