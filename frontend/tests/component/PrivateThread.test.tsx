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

/** An NDJSON stream that holds open until `release()` is called, so tests can
 *  observe the in-flight bubble instead of racing the completed turn. */
function heldStream(lines: object[]) {
  let release!: () => void;
  const gate = new Promise<void>((r) => {
    release = r;
  });
  const body = new ReadableStream({
    async start(controller) {
      const enc = new TextEncoder();
      for (const line of lines) {
        controller.enqueue(enc.encode(JSON.stringify(line) + '\n'));
      }
      await gate;
      controller.close();
    },
  });
  return { body, release };
}

describe('sending a turn', () => {
  test('calls the stream endpoint and inserts nothing client-side', async () => {
    let turnBody: unknown = null;
    server.use(
      http.post(`${BASE}/agent/turn/stream`, async ({ request }) => {
        turnBody = await request.json();
        return new HttpResponse(
          JSON.stringify({ type: 'done', reply_message_id: 'm-a' }) + '\n',
          { headers: { 'Content-Type': 'application/x-ndjson' } },
        );
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
      http.post(`${BASE}/agent/turn/stream`, () =>
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

test('accumulates streamed text in the pending bubble while the turn runs', async () => {
  const { body, release } = heldStream([
    { type: 'run', run_id: 'r1' },
    { type: 'tool_call', tool: 'team_get_state' },
    { type: 'text', text: 'The demo ' },
    { type: 'text', text: 'is Friday.' },
  ]);
  server.use(
    http.post(`${BASE}/agent/turn/stream`, () =>
      new HttpResponse(body, { headers: { 'Content-Type': 'application/x-ndjson' } }),
    ),
  );
  const user = userEvent.setup();
  renderInApp(<PrivateThread />);
  await user.type(screen.getByPlaceholderText(/this stays private/), 'when is the demo?');
  await user.click(screen.getByRole('button', { name: 'SEND' }));

  // visible mid-flight, assembled from two separate frames
  expect(await screen.findByText('The demo is Friday.')).toBeInTheDocument();
  expect(supaState.inserts).toHaveLength(0); // server persists, not the client

  release();
  // once the turn ends the bubble clears — the real message arrives via refresh
  await waitFor(() => {
    expect(screen.queryByText('The demo is Friday.')).not.toBeInTheDocument();
  });
});

test('says what the agent is doing, in words, before any text arrives', async () => {
  // This used to assert /checking memory_read_page/ — the raw tool identifier,
  // which is what the screen actually printed. Honest and unreadable, and
  // wrong for the half of the tools that are not "checks" at all: the same
  // template rendered "checking team_propose_task" for something that drafts
  // a task and waits for your key.
  const { body, release } = heldStream([
    { type: 'run', run_id: 'r1' },
    { type: 'tool_call', tool: 'memory_read_page' },
  ]);
  server.use(
    http.post(`${BASE}/agent/turn/stream`, () =>
      new HttpResponse(body, { headers: { 'Content-Type': 'application/x-ndjson' } }),
    ),
  );
  const user = userEvent.setup();
  renderInApp(<PrivateThread />);
  await user.type(screen.getByPlaceholderText(/this stays private/), 'what did we decide?');
  await user.click(screen.getByRole('button', { name: 'SEND' }));

  expect(await screen.findByText(/reading the wiki/)).toBeInTheDocument();
  expect(screen.queryByText(/memory_read_page/)).not.toBeInTheDocument();
  release();
});


test('an empty turn says so instead of just stopping', async () => {
  // 🔴 Six of fourteen live turns came back with no events at all. Because the
  // server persists a reply only `if reply`, nothing was written and nothing
  // rendered: the typing dots stopped and that was the whole answer, which is
  // exactly what a hang looks like. The run row said 'done'.
  const enc = new TextEncoder();
  const body = new ReadableStream({
    start(controller) {
      for (const line of [
        { type: 'run', run_id: 'r1' },
        {
          type: 'empty',
          run_id: 'r1',
          detail:
            'Comrade had nothing to say that time — the model came back empty.'
            + ' Nothing was changed. Try asking again.',
        },
        { type: 'done', user_message_id: 'm-u', reply_message_id: null },
      ]) {
        controller.enqueue(enc.encode(`${JSON.stringify(line)}
`));
      }
      controller.close();
    },
  });
  server.use(
    http.post(`${BASE}/agent/turn/stream`, () =>
      new HttpResponse(body, { headers: { 'Content-Type': 'application/x-ndjson' } }),
    ),
  );
  const user = userEvent.setup();
  renderInApp(<PrivateThread />);
  await user.type(screen.getByPlaceholderText(/this stays private/), 'what did we decide?');
  await user.click(screen.getByRole('button', { name: 'SEND' }));

  const note = await screen.findByText(/came back empty/);
  expect(note).toBeInTheDocument();
  // It survives the end of the turn — the frames all arrive and the stream
  // closes, and the explanation has to still be there afterwards or it is no
  // better than the silence it replaces.
  expect(await screen.findByText(/Nothing was changed/)).toBeInTheDocument();
  // Not the error slot: nothing the member did failed.
  expect(note).not.toHaveStyle({ color: 'var(--terracotta)' });
});
