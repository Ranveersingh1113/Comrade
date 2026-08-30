/** RLS proofs, from the CLIENT's side: message visibility. */
import { afterAll, beforeAll, describe, expect, test } from 'vitest';
import {
  cleanupTeam, createTeam, createUser, stackUp, type TestUser,
} from './harness';

describe.skipIf(!stackUp())('messages RLS through supabase-js', () => {
  let leader: TestUser;
  let member: TestUser;
  let teamId: string;

  beforeAll(async () => {
    leader = await createUser('msg-lead');
    member = await createUser('msg-mem');
    teamId = await createTeam(leader, [member]);
  });

  afterAll(async () => {
    await cleanupTeam(teamId, [leader, member]);
  });

  test('a group message is visible to every member', async () => {
    const { error } = await leader.client.from('messages').insert({
      team_id: teamId, thread_type: 'group', sender_kind: 'user',
      sender_id: leader.id, body: 'kickoff at noon',
    });
    expect(error).toBeNull();

    const { data } = await member.client
      .from('messages').select('body').eq('team_id', teamId).eq('thread_type', 'group');
    expect(data?.map((r) => r.body)).toContain('kickoff at noon');
  });

  test('a private thread is INVISIBLE to a teammate — including via raw select', async () => {
    const { error } = await member.client.from('messages').insert({
      team_id: teamId, thread_type: 'private', thread_owner_id: member.id,
      sender_kind: 'user', sender_id: member.id, body: 'private worry',
    });
    expect(error).toBeNull();

    // The leader queries with NO thread filter — RLS itself must hide it.
    const { data } = await leader.client
      .from('messages').select('body').eq('team_id', teamId);
    expect(data?.map((r) => r.body)).not.toContain('private worry');

    // The owner still sees their own.
    const { data: own } = await member.client
      .from('messages').select('body').eq('thread_owner_id', member.id);
    expect(own?.map((r) => r.body)).toContain('private worry');
  });

  test('nobody can post as someone else', async () => {
    const { error } = await member.client.from('messages').insert({
      team_id: teamId, thread_type: 'group', sender_kind: 'user',
      sender_id: leader.id, // forged sender
      body: 'impersonation attempt',
    });
    expect(error).not.toBeNull();
  });

  test('an outsider sees nothing at all', async () => {
    const outsider = await createUser('msg-out');
    try {
      const { data } = await outsider.client
        .from('messages').select('id').eq('team_id', teamId);
      expect(data).toEqual([]);
    } finally {
      await cleanupTeam('00000000-0000-0000-0000-000000000000', [outsider]);
    }
  });
});
