/**
 * Fixtures for the real-stack layer. Test users are created THROUGH GoTrue
 * (password grant), so their sessions carry genuine ES256 tokens — the mocked
 * layers can't prove that; this one does. Admin SQL (dev-only, never shipped
 * to the browser) seeds team structure and cleans up.
 */
import { createClient, type SupabaseClient } from '@supabase/supabase-js';
import { Client as PgClient } from 'pg';
import { randomBytes } from 'node:crypto';

export const PASSWORD = 'fe-test-password-1';

export interface TestUser {
  id: string;
  email: string;
  client: SupabaseClient;
}

function supabaseUrl(): string {
  return (process.env.VITE_SUPABASE_URL ?? '').replace(/\/$/, '');
}

export function stackUp(): boolean {
  return Boolean(process.env.VITE_SUPABASE_URL && process.env.COMRADE_DB_URL_ADMIN);
}

export async function adminSql(): Promise<PgClient> {
  const client = new PgClient({ connectionString: process.env.COMRADE_DB_URL_ADMIN });
  await client.connect();
  return client;
}

function anonClient(): SupabaseClient {
  return createClient(supabaseUrl(), process.env.VITE_SUPABASE_ANON_KEY ?? '', {
    auth: { persistSession: false, autoRefreshToken: false },
    realtime: { params: { eventsPerSecond: 20 } },
  });
}

/** GoTrue signup + sign-in; returns a client running as that user. */
export async function createUser(tag: string): Promise<TestUser> {
  const email = `fe-${tag}-${randomBytes(4).toString('hex')}@test.dev`;
  const client = anonClient();
  const { data, error } = await client.auth.signUp({ email, password: PASSWORD });
  if (error || !data.user) throw new Error(`signup failed: ${error?.message}`);
  const { error: signInErr } = await client.auth.signInWithPassword({
    email, password: PASSWORD,
  });
  if (signInErr) throw new Error(`sign-in failed: ${signInErr.message}`);
  return { id: data.user.id, email, client };
}

/** Team + active memberships via admin SQL. Leader first. */
export async function createTeam(leader: TestUser, members: TestUser[]): Promise<string> {
  const sql = await adminSql();
  try {
    const { rows } = await sql.query(
      'insert into public.teams (name, created_by) values ($1, $2) returning id',
      [`FE Test Team ${leader.email}`, leader.id],
    );
    const teamId: string = rows[0].id;
    await sql.query(
      "insert into public.memberships (team_id, user_id, role, status)"
      + " values ($1, $2, 'leader', 'active')",
      [teamId, leader.id],
    );
    for (const m of members) {
      await sql.query(
        "insert into public.memberships (team_id, user_id, role, status)"
        + " values ($1, $2, 'member', 'active')",
        [teamId, m.id],
      );
    }
    return teamId;
  } finally {
    await sql.end();
  }
}

export async function cleanupTeam(teamId: string, users: TestUser[]): Promise<void> {
  const sql = await adminSql();
  try {
    await sql.query('delete from public.teams where id = $1', [teamId]);
    for (const u of users) {
      await sql.query('delete from auth.users where id = $1', [u.id]);
      await u.client.removeAllChannels();
    }
  } finally {
    await sql.end();
  }
}
