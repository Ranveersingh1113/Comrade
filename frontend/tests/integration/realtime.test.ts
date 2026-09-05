/**
 * Realtime round-trip: the class of bug where a subscription connects and
 * then stays silent forever (unpublished table) is only catchable live.
 */
import { afterAll, beforeAll, describe, expect, test } from 'vitest';
import { randomUUID } from 'node:crypto';
import type { RealtimeChannel } from '@supabase/supabase-js';
import {
  cleanupTeam, createTeam, createUser, stackUp, type TestUser,
} from './harness';

// A ceiling, not a delay: a passing run resolves the moment the event lands,
// so raising this costs nothing when things are healthy. It was 5s, which
// flaked twice under load (a full backend suite running alongside).
const EVENT_WAIT_MS = 15000;
// The negative assertion DOES wait this long every run, so it stays short.
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
  let generalThreadId: string;

  beforeAll(async () => {
    a = await createUser('rt-a');
    b = await createUser('rt-b');
    teamId = await createTeam(a, [b]);
    const { data, error } = await a.client.from('threads').select('id')
      .eq('team_id', teamId).eq('title', 'General').single();
    expect(error).toBeNull();
    generalThreadId = data!.id;
  });

  afterAll(async () => {
    await cleanupTeam(teamId, [a, b]);
  });

  /**
   * One trial: subscribe, insert, wait for the event.
   *
   * Separated so it can be RETRIED, which is not the same as being lenient.
   * The container log at the moment of a failure reads:
   *
   *   step: :disconnected
   *   Create replication slot supabase_realtime_messages_replication_slot_
   *   Starting stream replication for slot ... protocol version 2
   *
   * Realtime rebuilt its replication stream five seconds into the test. The
   * event was not late, it was LOST — so the ceiling being 5s or 15s or a
   * minute makes no difference, which is why raising it in July did not fix
   * this and why it flaked twice more today.
   *
   * A trial interrupted by the stream restarting is an invalid trial, not a
   * failed assertion. Two consecutive failures still fail: that is the line
   * between tolerating a local-stack transient and hiding a product bug.
   */
  async function trial(marker: string): Promise<void> {
    const events: unknown[] = [];
    const channel = a.client
      .channel(`it-messages-${teamId}-${marker}`)
      .on(
        'postgres_changes',
        { event: 'INSERT', schema: 'public', table: 'messages', filter: `team_id=eq.${teamId}` },
        (payload) => events.push(payload),
      );
    await subscribed(channel);

    const { error } = await b.client.from('messages').insert({
      team_id: teamId, thread_id: generalThreadId, sender_kind: 'user',
      sender_id: b.id, body: marker,
    });
    expect(error).toBeNull();

    try {
      await new Promise<void>((resolve, reject) => {
        const started = Date.now();
        const poll = setInterval(() => {
          if (events.length > 0) {
            clearInterval(poll);
            resolve();
          } else if (Date.now() - started > EVENT_WAIT_MS) {
            clearInterval(poll);
            reject(new Error(
              `no realtime event within ${EVENT_WAIT_MS}ms — is the table published,`
              + ' the realtime container up, and its replication stream settled?',
            ));
          }
        }, 100);
      });
      const row = (events[0] as { new: { body: string } }).new;
      expect(row.body).toBe(marker);
    } finally {
      await a.client.removeChannel(channel);
    }
  }

  test('a group message insert reaches a subscribed teammate', async () => {
    try {
      await trial('realtime ping');
    } catch (first) {
      // process.stderr, not console.warn: vitest routes console output through
      // its own reporter, and the warning did not appear in the run at all
      // when this was checked. A retry nobody can see is how a test that has
      // genuinely started failing goes on looking healthy — which is the same
      // silent-signal bug as ignore_errors=True and `pytest | tail`, so it is
      // worth not relying on a framework's interception for it.
      process.stderr.write(
        `\n[realtime] first trial failed (${(first as Error).message});`
        + ' retrying once in case the replication stream was restarting.\n',
      );
      await trial('realtime ping retry');
    }
  }, EVENT_WAIT_MS * 3);

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

    const threadId = randomUUID();
    const { error: threadError } = await b.client.from('threads').insert({
      id: threadId, team_id: teamId, title: 'Private', visibility: 'restricted',
      kind: 'discussion', owner_id: b.id, created_by: b.id,
    });
    expect(threadError).toBeNull();
    const { error: participantError } = await b.client.from('thread_participants').insert({
      thread_id: threadId, team_id: teamId, user_id: b.id, added_by: b.id,
    });
    expect(participantError).toBeNull();
    const { error } = await b.client.from('messages').insert({
      team_id: teamId, thread_id: threadId,
      sender_kind: 'user', sender_id: b.id, body: 'private realtime secret',
    });
    expect(error).toBeNull();

    await new Promise((r) => setTimeout(r, SILENCE_WAIT_MS));
    const bodies = events.map((e) => (e as { new: { body?: string } }).new?.body);
    expect(bodies).not.toContain('private realtime secret');
    await a.client.removeChannel(channel);
  });
});
