-- RLS isolation test — adversarial checks that the boundaries actually hold.
-- Run: Get-Content tests/rls_isolation_test.sql -Raw |
--        docker exec -i supabase_db_Comrade psql -U postgres -d postgres -v ON_ERROR_STOP=1
-- Everything runs in ONE transaction and is rolled back at the end (idempotent).
-- Any `raise exception 'FAIL...'` aborts with ON_ERROR_STOP => non-zero exit.

\set ON_ERROR_STOP on
begin;

-- Allow this test session to impersonate the worker roles (postgres is not a
-- superuser on Supabase local, and is not a member of the custom roles).
-- Transactional => undone by the rollback at the end.
grant comrade_agent to postgres;
grant comrade_pipeline to postgres;

-- ---------- fixed ids ----------
-- teamA = aaaaaaaa..., teamB = bbbbbbbb...
-- A1 leader/A2 member of team A; B1 leader/B2 member of team B.

-- ---------- seed (as postgres; RLS does not apply to superuser owner) ----------
insert into auth.users (instance_id, id, aud, role, email, encrypted_password, created_at, updated_at)
values
  ('00000000-0000-0000-0000-000000000000','a1a1a1a1-0000-0000-0000-000000000001','authenticated','authenticated','a1@test.dev','',now(),now()),
  ('00000000-0000-0000-0000-000000000000','a2a2a2a2-0000-0000-0000-000000000002','authenticated','authenticated','a2@test.dev','',now(),now()),
  ('00000000-0000-0000-0000-000000000000','b1b1b1b1-0000-0000-0000-000000000001','authenticated','authenticated','b1@test.dev','',now(),now()),
  ('00000000-0000-0000-0000-000000000000','b2b2b2b2-0000-0000-0000-000000000002','authenticated','authenticated','b2@test.dev','',now(),now());

insert into public.profiles (id, display_name) values
  ('a1a1a1a1-0000-0000-0000-000000000001','A1'),
  ('a2a2a2a2-0000-0000-0000-000000000002','A2'),
  ('b1b1b1b1-0000-0000-0000-000000000001','B1'),
  ('b2b2b2b2-0000-0000-0000-000000000002','B2');

insert into public.teams (id, name, created_by) values
  ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','Team A','a1a1a1a1-0000-0000-0000-000000000001'),
  ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb','Team B','b1b1b1b1-0000-0000-0000-000000000001');

insert into public.memberships (team_id, user_id, role, status) values
  ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','a1a1a1a1-0000-0000-0000-000000000001','leader','active'),
  ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','a2a2a2a2-0000-0000-0000-000000000002','member','active'),
  ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb','b1b1b1b1-0000-0000-0000-000000000001','leader','active'),
  ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb','b2b2b2b2-0000-0000-0000-000000000002','member','active');

-- A1's PRIVATE thread message (only A1 should ever read this)
insert into public.messages (team_id, thread_type, thread_owner_id, sender_kind, sender_id, body) values
  ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','private','a1a1a1a1-0000-0000-0000-000000000001','user','a1a1a1a1-0000-0000-0000-000000000001','A1 private note');
-- a group message in team A
insert into public.messages (team_id, thread_type, sender_kind, sender_id, body) values
  ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','group','user','a2a2a2a2-0000-0000-0000-000000000002','hello team A');

-- memory entry+version in team A
insert into public.memory_entries (id, team_id) values
  ('e0000000-0000-0000-0000-0000000000e1','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa');
insert into public.memory_versions (id, entry_id, team_id, fact, change_type) values
  ('f0000000-0000-0000-0000-0000000000f1','e0000000-0000-0000-0000-0000000000e1','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','deadline is Friday','added');

-- a backend job in team A
insert into public.jobs (team_id, job_type) values
  ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','parse_document');

-- helper to impersonate an end user
\set claimsA1 '{"sub":"a1a1a1a1-0000-0000-0000-000000000001","role":"authenticated"}'
\set claimsA2 '{"sub":"a2a2a2a2-0000-0000-0000-000000000002","role":"authenticated"}'

-- ============================================================
-- T1: A2 sees ONLY team A (cross-team isolation)
-- ============================================================
set local role authenticated;
set local request.jwt.claims = :'claimsA2';
do $$
begin
  if (select count(*) from public.teams) <> 1 then
    raise exception 'FAIL T1a: A2 sees % teams (expected 1)', (select count(*) from public.teams);
  end if;
  if exists (select 1 from public.teams where id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb') then
    raise exception 'FAIL T1b: A2 can see team B';
  end if;
end $$;
reset role; reset request.jwt.claims;

-- ============================================================
-- T2: A2 CANNOT read A1's private thread; A1 CAN read own
-- ============================================================
set local role authenticated;
set local request.jwt.claims = :'claimsA2';
do $$
begin
  if exists (select 1 from public.messages where thread_type='private') then
    raise exception 'FAIL T2a: A2 can read a private thread that is not theirs';
  end if;
end $$;
reset role; reset request.jwt.claims;

set local role authenticated;
set local request.jwt.claims = :'claimsA1';
do $$
begin
  if (select count(*) from public.messages where thread_type='private') <> 1 then
    raise exception 'FAIL T2b: A1 cannot read own private thread';
  end if;
end $$;
reset role; reset request.jwt.claims;

-- ============================================================
-- T3: a member CANNOT author memory (insert into memory_versions denied)
-- ============================================================
set local role authenticated;
set local request.jwt.claims = :'claimsA1';
do $$
begin
  begin
    insert into public.memory_versions (entry_id, team_id, fact, change_type)
      values ('e0000000-0000-0000-0000-0000000000e1','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','injected fact','added');
    raise exception 'FAIL T3: authenticated member wrote memory_versions';
  exception when insufficient_privilege then null;  -- expected (RLS, no insert policy)
  end;
end $$;
reset role; reset request.jwt.claims;

-- ============================================================
-- T4: a member CAN trigger a revert (insert into memory_reverts)
-- ============================================================
set local role authenticated;
set local request.jwt.claims = :'claimsA1';
do $$
begin
  insert into public.memory_reverts (entry_id, team_id, member_id, reverted_version_id)
    values ('e0000000-0000-0000-0000-0000000000e1','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
            'a1a1a1a1-0000-0000-0000-000000000001','f0000000-0000-0000-0000-0000000000f1');
  -- if this raised, the test fails naturally
end $$;
reset role; reset request.jwt.claims;

-- ============================================================
-- T5: jobs are invisible to end users
-- ============================================================
set local role authenticated;
set local request.jwt.claims = :'claimsA1';
do $$
begin
  if (select count(*) from public.jobs) <> 0 then
    raise exception 'FAIL T5: end user can see jobs (expected 0)';
  end if;
end $$;
reset role; reset request.jwt.claims;

-- ============================================================
-- T6: comrade_agent (team A) reads memory but CANNOT write it
-- ============================================================
set local role comrade_agent;
set local app.current_team_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
do $$
begin
  if (select count(*) from public.memory_versions) <> 1 then
    raise exception 'FAIL T6a: agent cannot read team A memory';
  end if;
  begin
    insert into public.memory_versions (entry_id, team_id, fact, change_type)
      values ('e0000000-0000-0000-0000-0000000000e1','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','agent fact','added');
    raise exception 'FAIL T6b: agent wrote memory_versions';
  exception when insufficient_privilege then null;  -- expected (no insert grant)
  end;
end $$;
reset role; reset app.current_team_id;

-- ============================================================
-- T7: comrade_agent scoped to team A cannot see team B
-- ============================================================
set local role comrade_agent;
set local app.current_team_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
do $$
begin
  if exists (select 1 from public.teams where id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb') then
    raise exception 'FAIL T7: agent scoped to A can see team B';
  end if;
end $$;
reset role; reset app.current_team_id;

-- ============================================================
-- T8: comrade_pipeline (compiler) CAN write memory in team A
-- ============================================================
set local role comrade_pipeline;
set local app.current_team_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
do $$
begin
  insert into public.memory_versions (entry_id, team_id, fact, change_type)
    values ('e0000000-0000-0000-0000-0000000000e1','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','compiled fact','revised');
  -- success expected; any error fails the test
end $$;
reset role; reset app.current_team_id;

-- ============================================================
-- T9: comrade_pipeline scoped to team A cannot write into team B
-- ============================================================
set local role comrade_pipeline;
set local app.current_team_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
do $$
begin
  begin
    insert into public.memory_entries (team_id) values ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb');
    raise exception 'FAIL T9: pipeline scoped to A wrote into team B';
  exception when insufficient_privilege then null;  -- expected (with check team scope)
  end;
end $$;
reset role; reset app.current_team_id;

\echo '======================================'
\echo 'ALL RLS ISOLATION CHECKS PASSED (T1-T9)'
\echo '======================================'

rollback;
