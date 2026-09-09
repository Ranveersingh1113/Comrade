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
 * 🔴 THE DEFECT (fix.md F47). A regression inside my own F09 fix.
 *
 * F09 released the composer during a run so a member could steer the turn they
 * started — the thing the backend has accepted since T15. What it did not
 * revisit is the failure path that was written for the FIRST send:
 *
 *     } catch (e) {
 *       setAiTyping(false);
 *       setSendError(agentErrorText(e));
 *       setDraft(text);
 *       return;
 *     }
 *
 * Clearing `aiTyping` is right when the POST that would have STARTED a run
 * fails: there is nothing to show. It is wrong for a steering send, because the
 * original run is still perfectly healthy and streaming — and the panel that
 * carries its partial answer and its STOP button is gated on that flag. So one
 * failed correction makes a live turn look finished: the text disappears, STOP
 * disappears, and later frames quietly append to a `pending` nobody is
 * rendering.
 *
 * The member is then looking at a room that says nothing is happening, while a
 * run they cannot stop spends their team's budget.
 */

const BASE = 'http://localhost:8000';
const thread = {
  id: 'thread-1', team_id: 'team-1', title: 'General', visibility: 'team' as const,
  kind: 'discussion' as const, work_state: null, owner_id: null, created_by: 'u1',
  created_at: '2026-07-20T09:00:00Z', updated_at: '2026-07-20T09:00:00Z',
};

/** A run that is still going, the way a live one is. */
const held = (lines: object[]) => new HttpResponse(
  new ReadableStream({
    start(controller) {
      const enc = new TextEncoder();
      for (const line of lines) {
        controller.enqueue(enc.encode(`${JSON.stringify(line)}\n`));
      }
      // Never closed.
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
  server.use(http.get(`${BASE}/threads/thread-1/agent-runs`, () =>
    HttpResponse.json([])));
});
afterAll(() => server.close());

/**
 * Ask Comrade something, leave the run streaming, then fail the next POST.
 */
async function withFailingSteering() {
  let posts = 0;
  server.use(
    http.post(`${BASE}/agent/turn`, () => {
      posts += 1;
      if (posts === 1) {
        return HttpResponse.json({ run_id: 'run-1', status: 'queued' });
      }
      // The correction never lands: a dropped connection, a 500, a proxy.
      return HttpResponse.error();
    }),
    http.get(`${BASE}/agent/runs/run-1/stream`, () =>
      held([{ type: 'run', run_id: 'run-1', status: 'running' },
            { seq: 0, type: 'text', text: 'halfway through the answer' }])),
  );

  const user = userEvent.setup();
  renderInApp(<GroupRoom thread={thread} allowTeamMessages />);
  await user.type(
    await screen.findByPlaceholderText(/Message the team/),
    '@comrade refactor the parser',
  );
  await user.click(screen.getByRole('button', { name: 'SEND' }));

  // The run is live and its partial answer is on screen.
  await screen.findByText('halfway through the answer');
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'SEND' })).toBeEnabled());

  // Now a correction that fails.
  await user.type(
    screen.getByPlaceholderText(/Message the team/), '@comrade not that file',
  );
  await user.click(screen.getByRole('button', { name: 'SEND' }));

  return user;
}

// ---------------------------------------------------------------------------

describe('a steering send that fails', () => {
  test('🔴 the live run keeps its partial answer on screen', async () => {
    await withFailingSteering();

    await waitFor(() => expect(screen.getByText(/No connection to Comrade/))
      .toBeInTheDocument());
    expect(screen.getByText('halfway through the answer')).toBeInTheDocument();
  });

  test('🔴 STOP is still there for the run that is still going', async () => {
    await withFailingSteering();

    await waitFor(() => expect(screen.getByText(/No connection to Comrade/))
      .toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'STOP' })).toBeEnabled();
  });

  test('the error is shown and the correction is given back', async () => {
    await withFailingSteering();

    await waitFor(() => expect(screen.getByText(/No connection to Comrade/))
      .toBeInTheDocument());
    expect(screen.getByPlaceholderText(/Message the team/))
      .toHaveValue('@comrade not that file');
  });

  test('the correction can be retried without restarting the run', async () => {
    let attaches = 0;
    let posts = 0;
    server.use(
      http.post(`${BASE}/agent/turn`, () => {
        posts += 1;
        if (posts === 1) {
          return HttpResponse.json({ run_id: 'run-1', status: 'queued' });
        }
        if (posts === 2) return HttpResponse.error();
        return HttpResponse.json({ run_id: 'run-1', status: 'steering' });
      }),
      http.get(`${BASE}/agent/runs/run-1/stream`, () => {
        attaches += 1;
        return held([{ seq: 0, type: 'text', text: 'still working' }]);
      }),
    );

    const user = userEvent.setup();
    renderInApp(<GroupRoom thread={thread} allowTeamMessages />);
    await user.type(
      await screen.findByPlaceholderText(/Message the team/), '@comrade go',
    );
    await user.click(screen.getByRole('button', { name: 'SEND' }));
    await screen.findByText('still working');

    await user.type(
      screen.getByPlaceholderText(/Message the team/), '@comrade wait',
    );
    await user.click(screen.getByRole('button', { name: 'SEND' }));
    await waitFor(() => expect(posts).toBe(2));

    // The draft came back, so retrying is one click.
    await user.click(screen.getByRole('button', { name: 'SEND' }));

    await waitFor(() => expect(posts).toBe(3));
    expect(attaches).toBe(1);
    expect(screen.getAllByText('still working')).toHaveLength(1);
  });

  test('a FIRST send that fails still clears the indicator', async () => {
    // The half that must not change: nothing is running, so leaving the
    // indicator on would promise an answer that is never coming.
    server.use(
      http.post(`${BASE}/agent/turn`, () => HttpResponse.error()),
    );

    const user = userEvent.setup();
    renderInApp(<GroupRoom thread={thread} allowTeamMessages />);
    await user.type(
      await screen.findByPlaceholderText(/Message the team/), '@comrade hello',
    );
    await user.click(screen.getByRole('button', { name: 'SEND' }));

    await waitFor(() => expect(screen.getByText(/No connection to Comrade/))
      .toBeInTheDocument());
    expect(screen.queryByRole('button', { name: 'STOP' })).not.toBeInTheDocument();
  });
});
