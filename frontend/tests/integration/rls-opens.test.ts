/** RLS proofs: document opens are per-viewer; the summary shows counts only. */
import { afterAll, beforeAll, describe, expect, test } from 'vitest';
import {
  adminSql, cleanupTeam, createTeam, createUser, stackUp, type TestUser,
} from './harness';

describe.skipIf(!stackUp())('document_opens + summary through supabase-js', () => {
  let leader: TestUser;
  let member: TestUser;
  let outsider: TestUser;
  let teamId: string;
  let docId: string;

  beforeAll(async () => {
    leader = await createUser('doc-lead');
    member = await createUser('doc-mem');
    outsider = await createUser('doc-out');
    teamId = await createTeam(leader, [member]);

    const sql = await adminSql();
    try {
      const doc = await sql.query(
        "insert into public.documents (team_id, uploader_id, kind, filename)"
        + " values ($1, $2, 'text', 'spec.txt') returning id",
        [teamId, leader.id],
      );
      docId = doc.rows[0].id;
    } finally {
      await sql.end();
    }
  });

  afterAll(async () => {
    await cleanupTeam(teamId, [leader, member, outsider]);
  });

  test('a member records their own open; a teammate cannot see the row', async () => {
    const { error } = await member.client.from('document_opens').insert({
      document_id: docId, user_id: member.id, first_opened_at: new Date().toISOString(),
    });
    expect(error).toBeNull();

    const { data } = await leader.client
      .from('document_opens').select('user_id').eq('document_id', docId);
    // leader sees only their OWN rows (none) — never who else opened what
    expect(data).toEqual([]);
  });

  test('the summary gives counts to a non-opener, naming nobody', async () => {
    const { data, error } = await leader.client
      .from('document_opens_summary')
      .select('opens_count, member_count')
      .eq('document_id', docId);
    expect(error).toBeNull();
    expect(data).toEqual([{ opens_count: 1, member_count: 2 }]);
  });

  test('an outsider gets no summary row at all', async () => {
    const { data } = await outsider.client
      .from('document_opens_summary').select('document_id').eq('document_id', docId);
    expect(data).toEqual([]);
  });
});
