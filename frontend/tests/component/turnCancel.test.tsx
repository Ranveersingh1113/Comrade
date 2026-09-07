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
 * 🔴 THE DEFECT. There was no way to stop a turn. `cancel_run` existed in the
 * queue module and nothing reached it — no endpoint, no button — so a member
 * who asked the wrong question, or watched a turn head somewhere expensive,
 * could only wait it out.
 *
 * And the other half of the same story: resolving a consent card resumed the
 * run on the server and the room rejoined nothing, so the member answered and
 * then watched a thread where nothing happened.
 */

const BASE = 'http://localhost:8000';
const thread = {
  id: 'thread-1', team_id: 'team-1', title: 'General', visibility: 'team' as const,
  kind: 'discussion' as const, work_state: null, owner_id: null, created_by: 'u1',
  created_at: '2026-07-20T09:00:00Z', updated_at: '2026-07-20T09:00:00Z',
};

/** A stream that stays open, so the turn is observably in flight. */
const held = (lines: object[]) =>
  new HttpResponse(
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

describe('stopping a turn', () => {
  test('a member can stop the turn they asked for', async () => {
    let cancelled: string | null = null;
    server.use(
      http.post(`${BASE}/agent/turn`, () =>
        HttpResponse.json({ run_id: 'run-1', status: 'queued' })),
      http.get(`${BASE}/agent/runs/run-1/stream`, () =>
        held([{ type: 'run', run_id: 'run-1', status: 'running' },
              { seq: 0, type: 'tool_call', tool: 'repo_run' }])),
      http.post(`${BASE}/agent/runs/run-1/cancel`, ({ request }) => {
        cancelled = new URL(request.url).pathname;
        return HttpResponse.json({ status: 'cancelled', already_finished: false });
      }),
    );

    const user = userEvent.setup();
    renderInApp(<GroupRoom thread={thread} allowTeamMessages />);
    await user.type(
      await screen.findByPlaceholderText(/Message the team/), '@comrade read everything',
    );
    await user.click(screen.getByRole('button', { name: 'SEND' }));

    await user.click(await screen.findByRole('button', { name: 'STOP' }));

    await waitFor(() => expect(cancelled).toBe('/agent/runs/run-1/cancel'));
  });

  test('a teammate is not offered the stop for a run they did not ask for', async () => {
    // Seeing someone's turn is not standing for them. The server refuses it
    // too; not drawing the button is so nobody is invited to try.
    server.use(
      http.get(`${BASE}/threads/thread-1/agent-runs`, () =>
        HttpResponse.json([
          { id: 'run-theirs', status: 'running', requester_id: 'someone-else', steps: [] },
        ])),
      http.get(`${BASE}/agent/runs/run-theirs/stream`, () =>
        held([{ type: 'run', run_id: 'run-theirs', status: 'running' }])),
    );

    renderInApp(<GroupRoom thread={thread} allowTeamMessages />);

    // The turn IS being followed — the room shows it running — and that is
    // what makes the absence of the button meaningful rather than incidental.
    await waitFor(() =>
      expect(document.querySelector('[data-agent-typing]')).not.toBeNull());
    expect(screen.queryByRole('button', { name: 'STOP' })).toBeNull();
  });
});

describe('after a permission decision', () => {
  test('the room rejoins the run the card was holding open', async () => {
    supaState.tables.consent_queue = [{
      id: 'consent-1', team_id: 'team-1', thread_id: 'thread-1',
      agent_run_id: 'run-parked', requesting_member_id: 'u1',
      tool_name: 'task_create', tool_args: { title: 'Publish notes' },
      source_snippet: 'publish notes', action_hash: 'abc', status: 'pending',
      reversible: true, tier: 'T1', expires_at: null,
      created_at: '2026-09-04T10:00:00Z', resolved_at: null, resolution_reason: null,
    }];
    let rejoined = 0;
    server.use(
      http.post(`${BASE}/consent/consent-1/approve`, () =>
        HttpResponse.json({ status: 'executed' })),
      http.get(`${BASE}/agent/runs/run-parked/stream`, () => {
        rejoined += 1;
        return ndjson([
          { type: 'run', run_id: 'run-parked', status: 'running' },
          { seq: 0, type: 'text', text: 'carrying on then' },
          { type: 'done', run_id: 'run-parked', status: 'done' },
        ]);
      }),
    );

    const user = userEvent.setup();
    renderInApp(<GroupRoom thread={thread} allowTeamMessages />);
    await screen.findByTestId('consent-card');
    await user.click(screen.getByRole('button', { name: 'Approve once' }));

    await waitFor(() => expect(rejoined).toBe(1));
  });
});
