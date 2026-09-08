import {
  afterAll, afterEach, beforeAll, beforeEach, describe, expect, test, vi,
} from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import {
  makeSupabaseMock, makeTeamMock, renderInApp, resetSupa, resetTeam, server,
  supaState,
} from './mocks';

vi.mock('../../src/lib/supabase', () => makeSupabaseMock());
vi.mock('../../src/state/TeamContext', () => makeTeamMock());

import { GroupRoom } from '../../src/screens/GroupRoom';

/**
 * 🔴 THE DEFECT (fix.md F09). The composer was locked for the whole run.
 *
 * `send` held `sending` true until `deliver` returned, and `deliver` awaited
 * `follow()` — which watches the durable run to its end. So the SEND button
 * stayed disabled from the moment the question was asked until the answer was
 * complete, which on a long tool-using turn is minutes.
 *
 * The person who most needs to say something during a run is the one who
 * started it: "no, not that file", "stop after the tests". The backend has
 * supported exactly that since T15 — `enqueue_agent_turn` answers a send made
 * while a run is live with `disposition = 'steering'` and attaches the message
 * to the run in flight. The room could not reach it. The only way to type
 * again was to reload the page, which is also the thing that looks like giving
 * up on the turn.
 *
 * The guard belongs on ADMISSION — the window a double click can duplicate —
 * not on the stream's lifetime.
 *
 * The second half is subtler: steering hands back the id of the run ALREADY
 * being followed. Following it again restarts its stream from `after_seq=-1`
 * and replays every step card already on screen, so the fix is only correct if
 * the room keeps the watch it has.
 */

const BASE = 'http://localhost:8000';
const thread = {
  id: 'thread-1', team_id: 'team-1', title: 'General', visibility: 'team' as const,
  kind: 'discussion' as const, work_state: null, owner_id: null, created_by: 'u1',
  created_at: '2026-07-20T09:00:00Z', updated_at: '2026-07-20T09:00:00Z',
};

const ndjson = (lines: object[]) =>
  new HttpResponse(lines.map((l) => `${JSON.stringify(l)}\n`).join(''), {
    headers: { 'Content-Type': 'application/x-ndjson' },
  });

/** A stream that stays open, the way a real run in progress does. */
const held = (lines: object[]) => {
  const body = new ReadableStream({
    start(controller) {
      const enc = new TextEncoder();
      for (const line of lines) {
        controller.enqueue(enc.encode(`${JSON.stringify(line)}\n`));
      }
      // Deliberately never closed: this run has not finished.
    },
  });
  return new HttpResponse(body, {
    headers: { 'Content-Type': 'application/x-ndjson' },
  });
};

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
beforeEach(() => {
  localStorage.clear();
  resetSupa();
  resetTeam();
  server.use(http.get(`${BASE}/threads/thread-1/agent-runs`, () =>
    HttpResponse.json([])));
});
afterAll(() => server.close());

/** Ask Comrade something, and leave the run open. Returns the POST bodies. */
async function askWithRunHeldOpen() {
  const posts: Record<string, unknown>[] = [];
  let attaches = 0;
  server.use(
    http.post(`${BASE}/agent/turn`, async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      posts.push(body);
      // First send starts the run; anything sent while it is live is steering
      // and comes back carrying the SAME run id.
      return HttpResponse.json({
        run_id: 'run-1',
        status: posts.length === 1 ? 'queued' : 'steering',
      });
    }),
    http.get(`${BASE}/agent/runs/run-1/stream`, () => {
      attaches += 1;
      return held([{ type: 'run', run_id: 'run-1', status: 'running' },
                   { seq: 0, type: 'text', text: 'working on it' }]);
    }),
  );

  const user = userEvent.setup();
  renderInApp(<GroupRoom thread={thread} allowTeamMessages />);
  const box = await screen.findByPlaceholderText(/Message the team/);
  await user.type(box, '@comrade refactor the parser');
  await user.click(screen.getByRole('button', { name: 'SEND' }));

  // The run is live and streaming — this is the state the composer was stuck in.
  await waitFor(() => expect(posts).toHaveLength(1));
  await screen.findByText('working on it');

  return { user, posts, box, attaches: () => attaches };
}

// ---------------------------------------------------------------------------

describe('the composer during a run', () => {
  test('🔴 SEND is usable again once the message is durable', async () => {
    // The finding's own case. Admission is over; the answer is not.
    const { attaches } = await askWithRunHeldOpen();

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'SEND' })).toBeEnabled());
    expect(attaches()).toBe(1);
  });

  test('🔴 a correction typed during the run is accepted as steering', async () => {
    const { user, posts } = await askWithRunHeldOpen();

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'SEND' })).toBeEnabled());
    await user.type(
      screen.getByPlaceholderText(/Message the team/), '@comrade not that file',
    );
    await user.click(screen.getByRole('button', { name: 'SEND' }));

    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1].text).toBe('@comrade not that file');
  });

  test('steering keeps the watch it has instead of replaying the run', async () => {
    // The server answers steering with the id of the run already in flight.
    // Re-following it would ask for after_seq=-1 and put every step card on
    // screen a second time.
    const { user, posts, attaches } = await askWithRunHeldOpen();

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'SEND' })).toBeEnabled());
    await user.type(
      screen.getByPlaceholderText(/Message the team/), '@comrade stop after tests',
    );
    await user.click(screen.getByRole('button', { name: 'SEND' }));
    await waitFor(() => expect(posts).toHaveLength(2));

    expect(attaches()).toBe(1);
    expect(screen.getAllByText('working on it')).toHaveLength(1);
  });

  test('a correction carries its own attempt id, not the first send\'s', async () => {
    // The attempt id makes a RETRY safe. Reusing it for a deliberate second
    // message would make the server answer `duplicate` and drop the steering.
    const { user, posts } = await askWithRunHeldOpen();

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'SEND' })).toBeEnabled());
    await user.type(
      screen.getByPlaceholderText(/Message the team/), '@comrade actually stop',
    );
    await user.click(screen.getByRole('button', { name: 'SEND' }));

    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1].client_request_id).not.toBe(posts[0].client_request_id);
  });

  test('a rapid double click still asks the question once', async () => {
    // The protection that must survive the fix: the guard is narrower now,
    // but the window it covers is exactly the one a double click lands in.
    const posts: unknown[] = [];
    let release: (() => void) | undefined;
    const admitted = new Promise<void>((resolve) => { release = resolve; });
    server.use(
      http.post(`${BASE}/agent/turn`, async ({ request }) => {
        posts.push((await request.json()));
        await admitted;                       // still in flight, as on a slow link
        return HttpResponse.json({ run_id: 'run-1', status: 'queued' });
      }),
      http.get(`${BASE}/agent/runs/run-1/stream`, () =>
        ndjson([{ type: 'done', run_id: 'run-1', status: 'done' }])),
    );

    const user = userEvent.setup();
    renderInApp(<GroupRoom thread={thread} allowTeamMessages />);
    await user.type(
      await screen.findByPlaceholderText(/Message the team/), '@comrade deploy?',
    );
    const send = screen.getByRole('button', { name: 'SEND' });
    await user.click(send);
    await user.click(send);

    await waitFor(() => expect(posts).toHaveLength(1));
    release?.();
    // And it stays one after the POST resolves.
    await waitFor(() => expect(send).toBeEnabled());
    expect(posts).toHaveLength(1);
  });

  test('a team message during a run still goes to the thread', async () => {
    // Not every send is a question. A member talking to the team while the
    // agent works was locked out by the same flag.
    const { user } = await askWithRunHeldOpen();

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'SEND' })).toBeEnabled());
    await user.type(
      screen.getByPlaceholderText(/Message the team/), 'grabbing coffee',
    );
    await user.click(screen.getByRole('button', { name: 'SEND' }));

    await waitFor(() => expect(supaState.inserts).toContainEqual(
      expect.objectContaining({
        table: 'messages',
        values: expect.objectContaining({ body: 'grabbing coffee' }),
      }),
    ));
  });
});
