/** RLS proofs: consent visibility tiers + countersign column guard. */
import { afterAll, beforeAll, describe, expect, test } from 'vitest';
import {
  adminSql, cleanupTeam, createTeam, createUser, stackUp, type TestUser,
} from './harness';

describe.skipIf(!stackUp())('consent_queue RLS through supabase-js', () => {
  let leader: TestUser;
  let member: TestUser;
  let teamId: string;
  let t2Id: string;
  let t3Id: string;

  beforeAll(async () => {
    leader = await createUser('con-lead');
    member = await createUser('con-mem');
    teamId = await createTeam(leader, [member]);

    const sql = await adminSql();
    try {
      const t2 = await sql.query(
        "insert into public.consent_queue (team_id, requesting_member_id, tool_name,"
        + " tool_args, action_hash, tier) values ($1, $2, 'post_group_message',"
        + " '{\"body\":\"t2 post\"}', 'h2', 'T2') returning id",
        [teamId, leader.id],
      );
      t2Id = t2.rows[0].id;
      const t3 = await sql.query(
        "insert into public.consent_queue (team_id, requesting_member_id, tool_name,"
        + " tool_args, action_hash, tier) values ($1, $2, 'post_group_message',"
        + " '{\"body\":\"t3 post\"}', 'h3', 'T3') returning id",
        [teamId, leader.id],
      );
      t3Id = t3.rows[0].id;
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

  test('a teammate CAN see the pending T3 item (to countersign)', async () => {
    const { data } = await member.client
      .from('consent_queue').select('id, tier').eq('id', t3Id);
    expect(data).toEqual([{ id: t3Id, tier: 'T3' }]);
  });

  test('the teammate countersigns by writing ONLY the second-key column', async () => {
    const { data, error } = await member.client
      .from('consent_queue')
      .update({ second_approver_id: member.id })
      .eq('id', t3Id)
      .select('second_approver_id, second_approved_at');
    expect(error).toBeNull();
    expect(data?.[0]?.second_approver_id).toBe(member.id);
    expect(data?.[0]?.second_approved_at).toBeTruthy(); // trigger stamped it
  });

  test('a countersign that also touches tool_args is rejected by the trigger', async () => {
    const { error } = await member.client
      .from('consent_queue')
      .update({ second_approver_id: member.id, tool_args: { body: 'swapped' } })
      .eq('id', t3Id);
    expect(error?.message).toMatch(/only set the second key/);
  });

  test('the requester cannot forge their own countersign', async () => {
    // Fresh item: the shared one is already countersigned, and re-writing the
    // same value is (correctly) not a change the trigger objects to.
    const sql = await adminSql();
    let freshId: string;
    try {
      const fresh = await sql.query(
        "insert into public.consent_queue (team_id, requesting_member_id, tool_name,"
        + " tool_args, action_hash, tier) values ($1, $2, 'post_group_message',"
        + " '{\"body\":\"fresh t3\"}', 'h4', 'T3') returning id",
        [teamId, leader.id],
      );
      freshId = fresh.rows[0].id;
    } finally {
      await sql.end();
    }
    const { error } = await leader.client
      .from('consent_queue')
      .update({ second_approver_id: member.id })
      .eq('id', freshId);
    expect(error?.message).toMatch(/cannot set the second key/);
  });
});
