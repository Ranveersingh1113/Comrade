import {
  afterAll, afterEach, beforeAll, beforeEach, describe, expect, test, vi,
} from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import {
  makeSupabaseMock, makeTeamMock, renderInApp, resetSupa, resetTeam, server,
  supaState,
} from './mocks';

vi.mock('../../src/lib/supabase', () => makeSupabaseMock());
vi.mock('../../src/state/TeamContext', () => makeTeamMock());

import { GroupRoom } from '../../src/screens/GroupRoom';
import { agentErrorText, failureReference } from '../../src/lib/agentApi';

/**
 * 🔴 THE DEFECTS.
 *
 * `deleteForEveryone` awaited its update and looked at nothing. RLS refusing
 * the delete produced no error, no message, and a refresh that put the message
 * straight back — so "delete" looked like a UI that had not noticed the click.
 *
 * The draft lived in component state, so switching threads to check something
 * threw away whatever had been typed.
 *
 * Nothing stopped a second send. A double click, or an impatient second press
 * while the first was still in flight, sent the question twice.
 *
 * And the errors were written for whoever wrote the code: "Agent endpoint not
 * reachable — is the backend running on :8000?" is a sentence about somebody
 * else's laptop.
 */

const BASE = 'http://localhost:8000';
const thread = {
  id: 'thread-1', team_id: 'team-1', title: 'General', visibility: 'team' as const,
  kind: 'discussion' as const, work_state: null, owner_id: null, created_by: 'u1',
  created_at: '2026-07-20T09:00:00Z', updated_at: '2026-07-20T09:00:00Z',
};

const msg = (over: Record<string, unknown> = {}) => ({
  id: 'm-1', team_id: 'team-1', thread_id: 'thread-1', sender_kind: 'user',
  sender_id: 'u1', body: 'hello team', deleted_scope: null, deleted_by: null,
  deleted_at: null, created_at: '2026-07-20T09:00:00Z', ...over,
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

describe('an action that did not work', () => {
  test('a refused delete says so instead of looking like a missed click', async () => {
    supaState.tables.messages = [msg()];
    supaState.updateErrors = { messages: 'row-level security refused this' };
    renderInApp(<GroupRoom thread={thread} allowTeamMessages />);

    const row = (await screen.findAllByText('hello team'))[0];
    // fireEvent rather than userEvent: the control only exists while the row
    // is hovered, and moving a synthetic pointer onto it can drop the hover
    // that renders it.
    fireEvent.mouseEnter(row.closest('[data-message-side]') ?? row);
    fireEvent.click(await screen.findByTitle(/Remove for everyone/i));

    await waitFor(() => expect(document.body.textContent)
      .toMatch(/could not be deleted/i));
  });
});

describe('what has been typed', () => {
  test('a draft survives leaving the thread and coming back', async () => {
    const user = userEvent.setup();
    const { unmount } = renderInApp(<GroupRoom thread={thread} allowTeamMessages />);
    await user.type(
      await screen.findByPlaceholderText(/Message the team/), 'half a thought',
    );
    unmount();

    renderInApp(<GroupRoom thread={thread} allowTeamMessages />);

    expect(await screen.findByPlaceholderText(/Message the team/))
      .toHaveValue('half a thought');
  });

  test('a draft belongs to its own thread', async () => {
    const other = { ...thread, id: 'thread-2', title: 'Planning' };
    const user = userEvent.setup();
    const { unmount } = renderInApp(<GroupRoom thread={thread} allowTeamMessages />);
    await user.type(
      await screen.findByPlaceholderText(/Message the team/), 'about General',
    );
    unmount();

    server.use(http.get(`${BASE}/threads/thread-2/agent-runs`, () => HttpResponse.json([])));
    renderInApp(<GroupRoom thread={other} allowTeamMessages />);

    expect(await screen.findByPlaceholderText(/Message the team/)).toHaveValue('');
  });

  test('two quick presses of send post one message', async () => {
    // The attempt id makes a RETRY safe; it does not make an impatient second
    // press free, because the second press is a different attempt.
    let released: () => void = () => {};
    const gate = new Promise<void>((r) => { released = r; });
    server.use(http.post(`${BASE}/agent/turn`, async () => {
      await gate;
      return HttpResponse.json({ run_id: 'run-1', status: 'queued' });
    }));
    server.use(http.get(`${BASE}/agent/runs/run-1/stream`, () =>
      new HttpResponse('{"type":"done","run_id":"run-1","status":"done"}\n')));

    const user = userEvent.setup();
    renderInApp(<GroupRoom thread={thread} allowTeamMessages />);
    await user.type(
      await screen.findByPlaceholderText(/Message the team/), '@comrade go',
    );
    const send = screen.getByRole('button', { name: 'SEND' });
    await user.click(send);

    expect(send).toBeDisabled();
    released();
  });
});

describe('what a failure says', () => {
  test('an unreachable runtime offers the member something to do', () => {
    // 🔴 "is the backend running on :8000?" is a sentence about somebody
    // else's laptop.
    const text = agentErrorText(new TypeError('Failed to fetch'));

    expect(text).not.toMatch(/:8000|backend running/i);
    expect(text).toMatch(/connection|try again|offline/i);
  });

  test('a failure carries a reference the member can quote', () => {
    const text = agentErrorText(new TypeError('Failed to fetch'));

    expect(text).toMatch(/[A-Za-z0-9]{6}/);
  });

  test('the technical detail goes to the log, not the room', () => {
    const logged: unknown[] = [];
    const original = console.error;
    console.error = (...args: unknown[]) => { logged.push(args); };
    try {
      const reference = failureReference(new TypeError('Failed to fetch'));
      expect(reference).toBeTruthy();
    } finally {
      console.error = original;
    }

    // An Error does not survive JSON.stringify, which is exactly why the
    // cause has to be logged as an object rather than folded into a string.
    const [tag, cause] = logged[0] as [string, unknown];
    expect(tag).toMatch(/comrade/i);
    expect((cause as Error).message).toBe('Failed to fetch');
  });
});
