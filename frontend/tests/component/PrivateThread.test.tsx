import { beforeEach, describe, expect, test, vi, afterAll, afterEach, beforeAll } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import {
  makeSupabaseMock, makeTeamMock, renderInApp, resetSupa, resetTeam, server, supaState,
} from './mocks';

vi.mock('../../src/lib/supabase', () => makeSupabaseMock());
vi.mock('../../src/state/TeamContext', () => makeTeamMock());

import { PrivateThread } from '../../src/screens/PrivateThread';

const BASE = 'http://localhost:8000';

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
beforeEach(() => {
  resetSupa();
  resetTeam();
});

test('renders the private exchange with the AI', async () => {
  supaState.tables.messages = [
    {
      id: 'p1', team_id: 'team-1', thread_type: 'private', thread_owner_id: 'u1',
      sender_kind: 'user', sender_id: 'u1', body: 'what should I pick up?',
      deleted_scope: null, deleted_by: null, deleted_at: null,
      created_at: '2026-07-20T09:00:00Z',
    },
    {
      id: 'p2', team_id: 'team-1', thread_type: 'private', thread_owner_id: 'u1',
      sender_kind: 'ai', sender_id: null, body: 'The API docs task is unclaimed.',
      deleted_scope: null, deleted_by: null, deleted_at: null,
      created_at: '2026-07-20T09:00:10Z',
    },
  ];
  renderInApp(<PrivateThread />);
  expect(await screen.findByText('what should I pick up?')).toBeInTheDocument();
  expect(screen.getByText('The API docs task is unclaimed.')).toBeInTheDocument();
  expect(screen.getByText(/Only you can see this/)).toBeInTheDocument();
});

describe('sending a turn', () => {
  test('calls /agent/turn and does NOT insert messages client-side', async () => {
    let turnBody: unknown = null;
    server.use(
      http.post(`${BASE}/agent/turn`, async ({ request }) => {
        turnBody = await request.json();
        return HttpResponse.json({
          run_id: 'r1', reply: 'On it.', user_message_id: 'm-u', reply_message_id: 'm-a',
        });
      }),
    );
    const user = userEvent.setup();
    renderInApp(<PrivateThread />);
    await user.type(
      screen.getByPlaceholderText(/this stays private/), 'draft a status update',
    );
    await user.click(screen.getByRole('button', { name: 'SEND' }));
    await waitFor(() => expect(turnBody).not.toBeNull());
    expect(turnBody).toEqual({
      team_id: 'team-1', text: 'draft a status update', thread_type: 'private',
    });
    // Server persists both sides; a client-side insert would double them.
    expect(supaState.inserts).toHaveLength(0);
  });

  test('a failed turn surfaces the error and restores the draft', async () => {
    server.use(
      http.post(`${BASE}/agent/turn`, () =>
        HttpResponse.json({ detail: 'not an active member' }, { status: 403 }),
      ),
    );
    const user = userEvent.setup();
    renderInApp(<PrivateThread />);
    const input = screen.getByPlaceholderText(/this stays private/);
    await user.type(input, 'hello?');
    await user.click(screen.getByRole('button', { name: 'SEND' }));
    expect(
      await screen.findByText(/not an active member of this team/),
    ).toBeInTheDocument();
    expect(input).toHaveValue('hello?');
  });
});
