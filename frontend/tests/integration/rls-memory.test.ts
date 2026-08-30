/** RLS proofs: the wiki is read-only for members; reverts are the ONE write. */
import { afterAll, beforeAll, describe, expect, test } from 'vitest';
import {
  adminSql, cleanupTeam, createTeam, createUser, stackUp, type TestUser,
} from './harness';

describe.skipIf(!stackUp())('memory RLS through supabase-js', () => {
  let leader: TestUser;
  let member: TestUser;
  let teamId: string;
  let entryId: string;
  let versionId: string;

  beforeAll(async () => {
    leader = await createUser('mem-lead');
    member = await createUser('mem-mem');
    teamId = await createTeam(leader, [member]);

    const sql = await adminSql();
    try {
      const page = await sql.query(
        "insert into public.memory_pages (team_id, title) values ($1, 'Deadlines') returning id",
        [teamId],
      );
      const entry = await sql.query(
        'insert into public.memory_entries (team_id, page_id) values ($1, $2) returning id',
        [teamId, page.rows[0].id],
      );
      entryId = entry.rows[0].id;
      const version = await sql.query(
        "insert into public.memory_versions (entry_id, team_id, fact, change_type)"
        + " values ($1, $2, 'Demo is Friday', 'added') returning id",
        [entryId, teamId],
      );
      versionId = version.rows[0].id;
    } finally {
      await sql.end();
    }
  });

  afterAll(async () => {
    await cleanupTeam(teamId, [leader, member]);
  });

  test('members read the wiki', async () => {
    const { data } = await member.client
      .from('memory_versions').select('fact').eq('team_id', teamId);
    expect(data?.map((r) => r.fact)).toContain('Demo is Friday');
  });

  test('members cannot write facts', async () => {
    const { error } = await member.client.from('memory_versions').insert({
      entry_id: entryId, team_id: teamId, fact: 'forged fact', change_type: 'added',
    });
    expect(error).not.toBeNull();
  });

  test('members cannot edit facts — the update silently changes nothing', async () => {
    const { data } = await member.client
      .from('memory_versions')
      .update({ fact: 'tampered' })
      .eq('id', versionId)
      .select();
    expect(data).toEqual([]); // RLS filtered the row out of the update

    const { data: check } = await member.client
      .from('memory_versions').select('fact').eq('id', versionId);
    expect(check?.[0]?.fact).toBe('Demo is Friday');
  });

  test('a member queues a revert AS THEMSELVES', async () => {
    const { error } = await member.client.from('memory_reverts').insert({
      entry_id: entryId, team_id: teamId, member_id: member.id,
      reverted_version_id: versionId,
    });
    expect(error).toBeNull();
  });

  test("a member cannot file a revert under someone else's name", async () => {
    const { error } = await member.client.from('memory_reverts').insert({
      entry_id: entryId, team_id: teamId, member_id: leader.id,
      reverted_version_id: versionId,
    });
    expect(error).not.toBeNull();
  });
});
