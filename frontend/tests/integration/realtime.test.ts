/**
 * Realtime round-trip: the class of bug where a subscription connects and
 * then stays silent forever (unpublished table) is only catchable live.
 */
import { afterAll, beforeAll, describe, expect, test } from 'vitest';
import type { RealtimeChannel } from '@supabase/supabase-js';
import {
  cleanupTeam, createTeam, createUser, stackUp, type TestUser,
} from './harness';

const EVENT_WAIT_MS = 5000;
const SILENCE_WAIT_MS = 2000;

function subscribed(channel: RealtimeChannel): Promise<void> {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('subscribe timed out')), EVENT_WAIT_MS);
    channel.subscribe((status) => {
      if (status === 'SUBSCRIBED') {
        clearTimeout(t);
        resolve();
      }
      if (status === 'CHANNEL_ERROR' || status === 'TIMED_OUT') {
        clearTimeout(t);
        reject(new Error(`subscription ${status}`));
      }
    });
  });
}

describe.skipIf(!stackUp())('postgres_changes round-trip', () => {
  let a: TestUser;
  let b: TestUser;
  let teamId: string;

  beforeAll(async () => {
    a = await createUser('rt-a');
    b = await createUser('rt-b');
    teamId = await createTeam(a, [b]);
  });

  afterAll(async () => {
    await cleanupTeam(teamId, [a, b]);
  });

  test('a group message insert reaches a subscribed teammate', async () => {
    const events: unknown[] = [];
    const channel = a.client
      .channel(`it-messages-${teamId}`)
      .on(
        'postgres_changes',
        { event: 'INSERT', schema: 'public', table: 'messages', filter: `team_id=eq.${teamId}` },
        (payload) => events.push(payload),
      );
    await subscribed(channel);

    const { error } = await b.client.from('messages').insert({
      team_id: teamId, thread_type: 'group', sender_kind: 'user',
      sender_id: b.id, body: 'realtime ping',
    });
    expect(error).toBeNull();

    await new Promise<void>((resolve, reject) => {
      const started = Date.now();
      const poll = setInterval(() => {
        if (events.length > 0) {
          clearInterval(poll);
          resolve();
        } else if (Date.now() - started > EVENT_WAIT_MS) {
          clearInterval(poll);
          reject(new Error(
            'no realtime event within 5s — is the table published and the realtime container up?',
          ));
        }
      }, 100);
    });

    const row = (events[0] as { new: { body: string } }).new;
    expect(row.body).toBe('realtime ping');
    await a.client.removeChannel(channel);
  });

  test("B's PRIVATE message never reaches A's subscription", async () => {
    const events: unknown[] = [];
    const channel = a.client
      .channel(`it-private-${teamId}`)
      .on(
        'postgres_changes',
        { event: 'INSERT', schema: 'public', table: 'messages', filter: `team_id=eq.${teamId}` },
        (payload) => events.push(payload),
      );
    await subscribed(channel);

    const { error } = await b.client.from('messages').insert({
      team_id: teamId, thread_type: 'private', thread_owner_id: b.id,
      sender_kind: 'user', sender_id: b.id, body: 'private realtime secret',
    });
    expect(error).toBeNull();

    await new Promise((r) => setTimeout(r, SILENCE_WAIT_MS));
    const bodies = events.map((e) => (e as { new: { body?: string } }).new?.body);
    expect(bodies).not.toContain('private realtime secret');
    await a.client.removeChannel(channel);
  });
});
