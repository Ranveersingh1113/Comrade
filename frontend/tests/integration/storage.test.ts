/** Storage RLS: the documents bucket is tenant-scoped by its first path segment. */
import { afterAll, beforeAll, describe, expect, test } from 'vitest';
import {
  cleanupTeam, createTeam, createUser, stackUp, type TestUser,
} from './harness';

describe.skipIf(!stackUp())('documents bucket RLS', () => {
  let member: TestUser;
  let outsider: TestUser;
  let teamId: string;

  beforeAll(async () => {
    member = await createUser('st-mem');
    outsider = await createUser('st-out');
    teamId = await createTeam(member, []);
  });

  afterAll(async () => {
    await cleanupTeam(teamId, [member, outsider]);
  });

  test('a member uploads into their own team folder', async () => {
    const { error } = await member.client.storage
      .from('documents')
      .upload(`${teamId}/spec.txt`, 'hello', { contentType: 'text/plain' });
    expect(error).toBeNull();
  });

  test('a non-member cannot upload into that folder', async () => {
    const { error } = await outsider.client.storage
      .from('documents')
      .upload(`${teamId}/intruder.txt`, 'nope', { contentType: 'text/plain' });
    expect(error).not.toBeNull();
  });

  test('a non-member cannot list the folder', async () => {
    const { data } = await outsider.client.storage.from('documents').list(teamId);
    expect(data ?? []).toEqual([]);
  });

  test('the bucket is private — no public URL serves the object', async () => {
    const { data } = member.client.storage
      .from('documents')
      .getPublicUrl(`${teamId}/spec.txt`);
    const res = await fetch(data.publicUrl);
    expect(res.ok).toBe(false);
  });
});
