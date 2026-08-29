/**
 * Seeds one team for the E2E journeys and writes ids to .state.json.
 * Reuses the integration harness (GoTrue users + admin SQL).
 */
import { writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
import { config as loadEnv } from 'dotenv';

export default async function globalSetup(): Promise<void> {
  loadEnv({ path: resolve(__dirname, '../../../.env'), quiet: true });
  loadEnv({ path: resolve(__dirname, '../../.env.local'), quiet: true });

  const { adminSql, createTeam, createUser, PASSWORD } = await import(
    '../integration/harness'
  );

  const leader = await createUser('e2e-lead');
  const member = await createUser('e2e-mem');
  const teamId = await createTeam(leader, [member]);

  const sql = await adminSql();
  try {
    // a proposed task for the member (journey: confirm)
    await sql.query(
      "insert into public.tasks (team_id, assignee_id, title, status,"
      + " created_by_kind, created_by_id) values ($1, $2, 'Draft the pilot survey',"
      + " 'proposed', 'user', $3)",
      [teamId, member.id, leader.id],
    );
    // a wiki fact (journey: revert)
    const page = await sql.query(
      "insert into public.memory_pages (team_id, title) values ($1, 'Deadlines') returning id",
      [teamId],
    );
    const entry = await sql.query(
      'insert into public.memory_entries (team_id, page_id) values ($1, $2) returning id',
      [teamId, page.rows[0].id],
    );
    // prior (inactive) + current version, so the wiki shows revision history
    // and its "restore" affordance — the revert journey's entry point
    await sql.query(
      "insert into public.memory_versions (entry_id, team_id, fact, change_type,"
      + " is_active, valid_from, valid_until) values"
      + " ($1, $2, 'Final demo is on Thursday', 'added', false,"
      + "  now() - interval '2 days', now() - interval '1 day')",
      [entry.rows[0].id, teamId],
    );
    await sql.query(
      "insert into public.memory_versions (entry_id, team_id, fact, change_type)"
      + " values ($1, $2, 'Final demo is on Friday', 'revised')",
      [entry.rows[0].id, teamId],
    );
    // a pending consent item for the leader (journey 5).
    // Hash must be genuine so execute passes: computed exactly as
    // shared/consent.compute_hash does.
    const { createHash } = await import('node:crypto');
    // Mirrors shared/consent.compute_hash: json.dumps(sort_keys=True,
    // separators=(',',':')) — compact JSON with keys in sorted order (args,
    // requester, team, tool; args' own keys below are already alphabetical:
    // assignee_id, title).
    const mkHash = (tool: string, team: string, requester: string, args: Record<string, unknown>) =>
      createHash('sha256')
        .update(JSON.stringify({ args, requester, team, tool }))
        .digest('hex');

    const t2Args = { assignee_id: member.id, title: 'Reminder: standup moved to 3pm.' };
    await sql.query(
      "insert into public.consent_queue (team_id, requesting_member_id, tool_name,"
      + " tool_args, action_hash, tier, expires_at)"
      + " values ($1, $2, 'task_create', $3, $4, 'T2', now() + interval '1 day')",
      [teamId, leader.id, JSON.stringify(t2Args),
       mkHash('task_create', teamId, leader.id, t2Args)],
    );
    // an AI observation in the room (journey: suppress)
    await sql.query(
      "insert into public.messages (team_id, thread_type, sender_kind, body)"
      + " values ($1, 'group', 'ai', 'Observation: the API doc has not moved in a week.')",
      [teamId],
    );
  } finally {
    await sql.end();
  }

  writeFileSync(
    resolve(__dirname, '.state.json'),
    JSON.stringify({
      teamId,
      leader: { id: leader.id, email: leader.email },
      member: { id: member.id, email: member.email },
      password: PASSWORD,
    }, null, 2),
  );
}
