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

/**
 * 🔴 THE DEFECT (fix.md F10). Only the TIDY interruption reconnected.
 *
 * T13 taught the room to reattach: a stream that ends without a terminal frame
 * comes back as `truncated`, and the loop follows again from the cursor. That
 * covers the case where the body closes cleanly mid-run.
 *
 * A real dropped connection does not do that. The socket dies, and `fetch` or
 * the body reader THROWS — a `TypeError`, no status, no frame. The catch
 * arm set an error and `break`, so the single most common way a connection
 * ends was the one case that never retried: the indicator went out, "No
 * connection to Comrade" appeared, and the run carried on answering nobody.
 *
 * Retrying is not unconditional. An abort is the member navigating away, and
 * 401/403/404 mean access is gone or the run is not there — hammering those
 * is noise. Exhaustion has to be reported as a lost WATCH, not a stopped run,
 * because the run is durable and very likely still going.
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

/** A body that dies partway through, the way a severed connection does.
 *
 *  Enqueued from `pull` rather than `start`: erroring a stream DISCARDS
 *  whatever is still queued, so writing the lines and the error together
 *  would deliver nothing at all and prove the wrong thing. */
const severed = (lines: object[]) => {
  const enc = new TextEncoder();
  let n = 0;
  const body = new ReadableStream({
    async pull(controller) {
      if (n < lines.length) {
        controller.enqueue(enc.encode(`${JSON.stringify(lines[n])}\n`));
        n += 1;
        return;
      }
      // Let the reader drain what is already queued. `error()` resets the
      // queue, so erroring the instant the last line is written would throw
      // it away and test a connection that delivered nothing.
      await new Promise((r) => { setTimeout(r, 30); });
      controller.error(new TypeError('network error'));
    },
  });
  return new HttpResponse(body, {
    headers: { 'Content-Type': 'application/x-ndjson' },
  });
};

/** A stream that stays open, the way a run still in progress does. */
const held = (lines: object[]) => new HttpResponse(
  new ReadableStream({
    start(controller) {
      const enc = new TextEncoder();
      for (const line of lines) {
        controller.enqueue(enc.encode(`${JSON.stringify(line)}\n`));
      }
    },
  }),
  { headers: { 'Content-Type': 'application/x-ndjson' } },
);

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
beforeEach(() => {
  localStorage.clear();
  resetSupa();
  resetTeam();
  server.use(
    http.get(`${BASE}/threads/thread-1/agent-runs`, () => HttpResponse.json([])),
    http.post(`${BASE}/agent/turn`, () =>
      HttpResponse.json({ run_id: 'run-1', status: 'queued' })),
  );
});
afterAll(() => server.close());

async function ask() {
  const user = userEvent.setup();
  renderInApp(<GroupRoom thread={thread} allowTeamMessages />);
  await user.type(
    await screen.findByPlaceholderText(/Message the team/), '@comrade status?',
  );
  await user.click(screen.getByRole('button', { name: 'SEND' }));
  return user;
}

// ---------------------------------------------------------------------------

describe('a stream that dies mid-run', () => {
  test('🔴 a thrown reader failure reconnects instead of ending the watch', async () => {
    let attach = 0;
    server.use(http.get(`${BASE}/agent/runs/run-1/stream`, () => {
      attach += 1;
      if (attach === 1) {
        return severed([{ type: 'run', run_id: 'run-1', status: 'running' },
                        { seq: 0, type: 'text', text: 'first half ' }]);
      }
      // Left open: this run has not finished, which is the whole reason to
      // reconnect to it.
      return held([{ seq: 1, type: 'text', text: 'second half' }]);
    }));

    await ask();

    await waitFor(() => expect(attach).toBe(2));
    // One continuous answer, not the first half twice.
    expect(await screen.findByText('first half second half')).toBeInTheDocument();
  });

  test('🔴 it resumes from the cursor, so nothing arrives twice', async () => {
    const cursors: (string | null)[] = [];
    let attach = 0;
    server.use(http.get(`${BASE}/agent/runs/run-1/stream`, ({ request }) => {
      cursors.push(new URL(request.url).searchParams.get('after_seq'));
      attach += 1;
      if (attach === 1) {
        return severed([{ seq: 0, type: 'text', text: 'aaa' },
                        { seq: 1, type: 'text', text: 'bbb' }]);
      }
      return ndjson([{ type: 'done', run_id: 'run-1', status: 'done' }]);
    }));

    await ask();

    await waitFor(() => expect(attach).toBe(2));
    expect(cursors).toEqual(['-1', '1']);
  });

  test('🔴 a reconnect does not submit the question again', async () => {
    // The run is durable. Asking again would put the same question in the
    // thread twice and charge the team for both.
    let posts = 0;
    let attach = 0;
    server.use(
      http.post(`${BASE}/agent/turn`, () => {
        posts += 1;
        return HttpResponse.json({ run_id: 'run-1', status: 'queued' });
      }),
      http.get(`${BASE}/agent/runs/run-1/stream`, () => {
        attach += 1;
        if (attach === 1) return severed([{ seq: 0, type: 'text', text: 'hi' }]);
        return ndjson([{ type: 'done', run_id: 'run-1', status: 'done' }]);
      }),
    );

    await ask();

    await waitFor(() => expect(attach).toBe(2));
    expect(posts).toBe(1);
  });

  test('a rejected initial fetch is retried the same way', async () => {
    // Nothing was received at all — the connection failed on the way out.
    let attach = 0;
    server.use(http.get(`${BASE}/agent/runs/run-1/stream`, () => {
      attach += 1;
      if (attach === 1) return HttpResponse.error();
      return held([{ seq: 0, type: 'text', text: 'the answer' }]);
    }));

    await ask();

    expect(await screen.findByText(/the answer/)).toBeInTheDocument();
  });

  test('revoked access is not retried', async () => {
    // 403 is an answer, not a hiccup. Reconnecting cannot change it.
    let attach = 0;
    server.use(http.get(`${BASE}/agent/runs/run-1/stream`, () => {
      attach += 1;
      return HttpResponse.json({ detail: 'not a member' }, { status: 403 });
    }));

    await ask();

    await waitFor(() =>
      expect(screen.getByText(/read-only for you/)).toBeInTheDocument());
    expect(attach).toBe(1);
  });

  test('a run that is not there is not retried', async () => {
    let attach = 0;
    server.use(http.get(`${BASE}/agent/runs/run-1/stream`, () => {
      attach += 1;
      return HttpResponse.json({ detail: 'no such run' }, { status: 404 });
    }));

    await ask();

    await waitFor(() =>
      expect(screen.getByText(/reference /)).toBeInTheDocument());
    expect(attach).toBe(1);
  });

  test('exhaustion is bounded, and says the watch was lost - not the run', async () => {
    // A connection that will not hold is worth reporting. Telling the member
    // the turn failed would be a lie: the run is leased and durable and is
    // most likely still working.
    let attach = 0;
    server.use(http.get(`${BASE}/agent/runs/run-1/stream`, () => {
      attach += 1;
      return severed([{ seq: attach - 1, type: 'text', text: `chunk ${attach}` }]);
    }));

    await ask();

    await waitFor(() => expect(screen.getByText(/reload to catch up/i))
      .toBeInTheDocument(), { timeout: 5000 });
    expect(attach).toBeLessThanOrEqual(5);
    expect(screen.queryByText(/Turn failed/)).not.toBeInTheDocument();
  });

  test('navigating away is not a transport failure', async () => {
    // Leaving the room aborts the read, which throws. Retrying that would
    // reattach to a run in a thread the member has left.
    let attach = 0;
    server.use(http.get(`${BASE}/agent/runs/run-1/stream`, () => {
      attach += 1;
      return new HttpResponse(
        new ReadableStream({
          start(controller) {
            controller.enqueue(new TextEncoder().encode(
              `${JSON.stringify({ seq: 0, type: 'text', text: 'working' })}\n`,
            ));
          },
        }),
        { headers: { 'Content-Type': 'application/x-ndjson' } },
      );
    }));

    const { unmount } = renderInApp(<GroupRoom thread={thread} allowTeamMessages />);
    const user = userEvent.setup();
    await user.type(
      await screen.findByPlaceholderText(/Message the team/), '@comrade status?',
    );
    await user.click(screen.getByRole('button', { name: 'SEND' }));
    await screen.findByText('working');

    unmount();
    await new Promise((r) => setTimeout(r, 300));

    expect(attach).toBe(1);
  });
});
