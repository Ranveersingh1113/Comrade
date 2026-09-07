import {
  afterAll, afterEach, beforeAll, beforeEach, describe, expect, test, vi,
} from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import {
  makeSupabaseMock, makeTeamMock, renderInApp, resetSupa, resetTeam, server,
} from './mocks';

vi.mock('../../src/lib/supabase', () => makeSupabaseMock());
vi.mock('../../src/state/TeamContext', () => makeTeamMock());

import { GroupRoom } from '../../src/screens/GroupRoom';
import { followRun, startTurn } from '../../src/lib/agentApi';

/**
 * 🔴 THE DEFECT. The browser held a run only in the memory of the POST that
 * started it, and that POST carried nothing identifying the attempt. Two
 * failures fell out of one design:
 *
 *  - A refresh, a sleeping laptop or a dropped proxy connection ended the
 *    stream, and nothing reconnected. The typing indicator stopped, no reply
 *    arrived, and a finished turn looked exactly like a severed one — while
 *    the run itself carried on, durable and leased, answering nobody.
 *  - An accepted POST whose response never arrived was indistinguishable from
 *    one that never landed. The room put the text back in the composer, the
 *    member pressed send again, and the thread got the same question twice.
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

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
beforeEach(() => {
  localStorage.clear();
  resetSupa();
  resetTeam();
  server.use(http.get(`${BASE}/threads/thread-1/agent-runs`, () => HttpResponse.json([])));
});
afterAll(() => server.close());

describe('following a durable run', () => {
  test('a stream that ends without a terminal frame is reported as truncated', async () => {
    // The case that had no representation at all. The old reader returned
    // normally whenever the body ended, so this was indistinguishable from a
    // completed turn — and the room announced the turn over.
    server.use(http.get(`${BASE}/agent/runs/r1/stream`, () =>
      ndjson([{ type: 'run', run_id: 'r1', status: 'running' },
              { seq: 0, type: 'text', text: 'half a th' }])));

    const outcome = await followRun('team-1', 'r1', () => {});

    expect(outcome).toBe('truncated');
  });

  test('a completed run is reported as done', async () => {
    server.use(http.get(`${BASE}/agent/runs/r1/stream`, () =>
      ndjson([{ type: 'run', run_id: 'r1', status: 'running' },
              { type: 'done', run_id: 'r1', status: 'done' }])));

    expect(await followRun('team-1', 'r1', () => {})).toBe('done');
  });

  test('a run waiting on a person is not reported as done', async () => {
    // It has not answered and it has not failed. Calling it done stops the
    // indicator and reads as a turn that died in silence, next to the consent
    // card that would have continued it.
    server.use(http.get(`${BASE}/agent/runs/r1/stream`, () =>
      ndjson([{ type: 'run', run_id: 'r1', status: 'running' },
              { type: 'status', run_id: 'r1', status: 'waiting_for_permission' }])));

    expect(await followRun('team-1', 'r1', () => {})).toBe('waiting');
  });

  test('reattaching asks only for events after the cursor', async () => {
    let asked: string | null = null;
    server.use(http.get(`${BASE}/agent/runs/r1/stream`, ({ request }) => {
      asked = new URL(request.url).searchParams.get('after_seq');
      return ndjson([{ type: 'done', run_id: 'r1', status: 'done' }]);
    }));

    await followRun('team-1', 'r1', () => {}, { afterSeq: 4 });

    expect(asked).toBe('4');
  });

  test('an aborted follow reports itself rather than throwing', async () => {
    // Leaving a room must not surface as a failed turn. It is also the only
    // thing navigation does: the run keeps working for everyone else.
    const control = new AbortController();
    server.use(http.get(`${BASE}/agent/runs/r1/stream`, () => {
      control.abort();
      return ndjson([{ type: 'done', run_id: 'r1', status: 'done' }]);
    }));

    expect(await followRun('team-1', 'r1', () => {}, { signal: control.signal }))
      .toBe('aborted');
  });
});

describe('submitting a turn', () => {
  test('a send carries an attempt id the server can recognise on retry', async () => {
    let body: Record<string, unknown> | undefined;
    server.use(http.post(`${BASE}/agent/turn`, async ({ request }) => {
      body = (await request.json()) as Record<string, unknown>;
      return HttpResponse.json({ run_id: 'r1', status: 'queued' });
    }));

    await startTurn('team-1', 'ship it?', 'thread-1', 'attempt-7');

    expect(body).toMatchObject({ client_request_id: 'attempt-7' });
  });
});

describe('the room', () => {
  test('a refresh mid-turn rejoins the run instead of showing a silent thread', async () => {
    // Held open, because a live run is the point: the reply is still arriving.
    const body = new ReadableStream({
      start(controller) {
        const enc = new TextEncoder();
        for (const line of [{ type: 'run', run_id: 'run-live', status: 'running' },
                            { seq: 0, type: 'text', text: 'still working on it' }]) {
          controller.enqueue(enc.encode(`${JSON.stringify(line)}\n`));
        }
      },
    });
    server.use(
      http.get(`${BASE}/threads/thread-1/agent-runs`, () =>
        HttpResponse.json([{ id: 'run-live', status: 'running', steps: [] }])),
      http.get(`${BASE}/agent/runs/run-live/stream`, () =>
        new HttpResponse(body, {
          headers: { 'Content-Type': 'application/x-ndjson' },
        })),
    );

    renderInApp(<GroupRoom thread={thread} allowTeamMessages />);

    // Reconstructed from server history alone — nothing was kept in the tab.
    expect(await screen.findByText('still working on it')).toBeInTheDocument();
  });

  test('a retry after an uncertain send reuses the same attempt id', async () => {
    // This is the whole point. The first POST is accepted and its response is
    // lost; the retry must be recognisable as the SAME submission, or the
    // member's question lands in the thread twice.
    const ids: unknown[] = [];
    let fail = true;
    server.use(
      http.post(`${BASE}/agent/turn`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        ids.push(body.client_request_id);
        if (fail) {
          fail = false;
          return HttpResponse.error();
        }
        return HttpResponse.json({ run_id: 'r1', status: 'duplicate' });
      }),
      http.get(`${BASE}/agent/runs/r1/stream`, () =>
        ndjson([{ type: 'done', run_id: 'r1', status: 'done' }])),
    );

    const user = userEvent.setup();
    renderInApp(<GroupRoom thread={thread} allowTeamMessages />);
    const box = await screen.findByPlaceholderText(/Message the team/);
    await user.type(box, '@comrade deploy?');
    await user.click(screen.getByRole('button', { name: 'SEND' }));

    // The failure restored the draft; sending it again is the retry.
    await waitFor(() => expect(ids).toHaveLength(1));
    await user.click(screen.getByRole('button', { name: 'SEND' }));

    await waitFor(() => expect(ids).toHaveLength(2));
    expect(ids[1]).toBe(ids[0]);
  });

  test('a truncated stream reattaches from the cursor rather than ending the turn', async () => {
    let attach = 0;
    const cursors: (string | null)[] = [];
    server.use(http.get(`${BASE}/agent/runs/r1/stream`, ({ request }) => {
      cursors.push(new URL(request.url).searchParams.get('after_seq'));
      attach += 1;
      if (attach === 1) {
        // Cut off. No terminal frame, no explanation — a proxy timeout.
        return ndjson([{ type: 'run', run_id: 'r1', status: 'running' },
                       { seq: 0, type: 'text', text: 'first half ' }]);
      }
      return ndjson([{ seq: 1, type: 'text', text: 'second half' },
                     { type: 'done', run_id: 'r1', status: 'done' }]);
    }));
    server.use(http.post(`${BASE}/agent/turn`, () =>
      HttpResponse.json({ run_id: 'r1', status: 'queued' })));

    const user = userEvent.setup();
    renderInApp(<GroupRoom thread={thread} allowTeamMessages />);
    await user.type(
      await screen.findByPlaceholderText(/Message the team/), '@comrade status?',
    );
    await user.click(screen.getByRole('button', { name: 'SEND' }));

    await waitFor(() => expect(attach).toBe(2));
    // Resumed where it stopped: replaying from zero would show the first half
    // of the answer twice.
    expect(cursors).toEqual(['-1', '0']);
  });
});
