/** RLS proofs, from the CLIENT's side: message visibility. */
import { afterAll, beforeAll, describe, expect, test } from 'vitest';
import { randomUUID } from 'node:crypto';
import {
  cleanupTeam, createTeam, createUser, stackUp, type TestUser,
} from './harness';

describe.skipIf(!stackUp())('messages RLS through supabase-js', () => {
  let leader: TestUser;
  let member: TestUser;
  let excludedMember: TestUser;
  let teamId: string;
  let generalThreadId: string;

  beforeAll(async () => {
    leader = await createUser('msg-lead');
    member = await createUser('msg-mem');
    excludedMember = await createUser('msg-excluded');
    teamId = await createTeam(leader, [member, excludedMember]);
    const { data, error } = await leader.client.from('threads').select('id')
      .eq('team_id', teamId).eq('title', 'General').single();
    expect(error).toBeNull();
    generalThreadId = data!.id;
  });

  afterAll(async () => {
    await cleanupTeam(teamId, [leader, member, excludedMember]);
  });

  test('a group message is visible to every member', async () => {
    const { error } = await leader.client.from('messages').insert({
      team_id: teamId, thread_id: generalThreadId, sender_kind: 'user',
      sender_id: leader.id, body: 'kickoff at noon',
    });
    expect(error).toBeNull();

    const { data } = await member.client
      .from('messages').select('body').eq('thread_id', generalThreadId);
    expect(data?.map((r) => r.body)).toContain('kickoff at noon');
  });

  test('a private thread is INVISIBLE to a teammate — including via raw select', async () => {
    const threadId = randomUUID();
    const { error: threadError } = await member.client.from('threads').insert({
      id: threadId, team_id: teamId, title: 'Private', visibility: 'restricted', kind: 'discussion',
      owner_id: member.id, created_by: member.id,
    });
    expect(threadError).toBeNull();
    const { error: participantError } = await member.client.from('thread_participants').insert({
      thread_id: threadId, team_id: teamId, user_id: member.id, added_by: member.id,
    });
    expect(participantError).toBeNull();
    const { error } = await member.client.from('messages').insert({
      team_id: teamId, thread_id: threadId,
      sender_kind: 'user', sender_id: member.id, body: 'private worry',
    });
    expect(error).toBeNull();

    // The leader queries with NO thread filter — RLS itself must hide it.
    const { data } = await leader.client
      .from('messages').select('body').eq('team_id', teamId);
    expect(data?.map((r) => r.body)).not.toContain('private worry');

    // The owner still sees their own.
    const { data: own } = await member.client.from('messages').select('body').eq('thread_id', threadId);
    expect(own?.map((r) => r.body)).toContain('private worry');
  });

  test('restricted thread metadata and messages stay hidden from an unselected member', async () => {
    const threadId = randomUUID();
    const { error: threadError } = await leader.client.from('threads').insert({
      id: threadId,
      team_id: teamId, title: 'salary review', visibility: 'restricted', kind: 'discussion',
      owner_id: leader.id, created_by: leader.id,
    });
    expect(threadError).toBeNull();
    const { error: participantError } = await leader.client.from('thread_participants').insert([
      { thread_id: threadId, team_id: teamId, user_id: leader.id, added_by: leader.id },
      { thread_id: threadId, team_id: teamId, user_id: member.id, added_by: leader.id },
    ]);
    expect(participantError).toBeNull();

    const { data: hiddenThreads } = await excludedMember.client
      .from('threads').select('id,title').eq('id', threadId);
    expect(hiddenThreads).toEqual([]);

    const { error: messageError } = await member.client.from('messages').insert({
      team_id: teamId, thread_id: threadId,
      sender_kind: 'user', sender_id: member.id, body: 'restricted update',
    });
    expect(messageError).toBeNull();
    const { data: hiddenMessages } = await excludedMember.client
      .from('messages').select('body').eq('thread_id', threadId);
    expect(hiddenMessages).toEqual([]);
  });

  test('nobody can post as someone else', async () => {
    const { error } = await member.client.from('messages').insert({
      team_id: teamId, thread_id: generalThreadId, sender_kind: 'user',
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
