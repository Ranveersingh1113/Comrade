/** RLS proof: consent visibility tiers. */
import { afterAll, beforeAll, describe, expect, test } from 'vitest';
import {
  adminSql, cleanupTeam, createTeam, createUser, stackUp, type TestUser,
} from './harness';

describe.skipIf(!stackUp())('consent_queue RLS through supabase-js', () => {
  let leader: TestUser;
  let member: TestUser;
  let teamId: string;
  let t2Id: string;

  beforeAll(async () => {
    leader = await createUser('con-lead');
    member = await createUser('con-mem');
    teamId = await createTeam(leader, [member]);

    const sql = await adminSql();
    try {
      const t2 = await sql.query(
        "insert into public.consent_queue (team_id, requesting_member_id, tool_name,"
        + " tool_args, action_hash, tier) values ($1, $2, 'task_create',"
        + " '{\"body\":\"t2 post\"}', 'h2', 'T2') returning id",
        [teamId, leader.id],
      );
      t2Id = t2.rows[0].id;
    } finally {
      await sql.end();
    }
  });

  afterAll(async () => {
    await cleanupTeam(teamId, [leader, member]);
  });

  test("a teammate cannot see the leader's T2 item", async () => {
    const { data } = await member.client
      .from('consent_queue').select('id').eq('id', t2Id);
    expect(data).toEqual([]);
  });
});
