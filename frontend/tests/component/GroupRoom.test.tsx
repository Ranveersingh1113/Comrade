import {
  afterAll, afterEach, beforeAll, beforeEach, describe, expect, test, vi,
} from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import {
  makeSupabaseMock, makeTeamMock, renderInApp, resetSupa, resetTeam, server,
  supaState,
} from './mocks';

vi.mock('../../src/lib/supabase', () => makeSupabaseMock());
vi.mock('../../src/state/TeamContext', () => makeTeamMock());

import { GroupRoom } from '../../src/screens/GroupRoom';

const msg = (over: Record<string, unknown>) => ({
  id: 'm-1', team_id: 'team-1', thread_type: 'group', thread_owner_id: null,
  sender_kind: 'user', sender_id: 'u1', body: 'hello team',
  deleted_scope: null, deleted_by: null, deleted_at: null,
  created_at: '2026-07-20T09:00:00Z',
  ...over,
});

const BASE = 'http://localhost:8000';

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
beforeEach(() => {
  resetSupa();
  resetTeam();
});
afterAll(() => server.close());

/** An NDJSON turn that stays open until release(), so a test can observe the
 *  in-flight line instead of racing the completed turn — `pending` and `step`
 *  are both cleared in the finally block, by design. */
function heldStream(lines: object[]) {
  let release!: () => void;
  const gate = new Promise<void>((r) => {
    release = r;
  });
  const body = new ReadableStream({
    async start(controller) {
      const enc = new TextEncoder();
      for (const line of lines) {
        controller.enqueue(enc.encode(`${JSON.stringify(line)}\n`));
      }
      await gate;
      controller.close();
    },
  });
  return { body, release };
}

/** One complete NDJSON turn, delivered and closed. */
function stream(lines: object[]) {
  const enc = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const line of lines) {
        controller.enqueue(enc.encode(`${JSON.stringify(line)}\n`));
      }
      controller.close();
    },
  });
}

async function askComrade(body: ReadableStream) {
  server.use(
    http.post(`${BASE}/agent/turn/stream`, () =>
      new HttpResponse(body, { headers: { 'Content-Type': 'application/x-ndjson' } }),
    ),
  );
  const user = userEvent.setup();
  renderInApp(<GroupRoom />);
  await user.type(
    await screen.findByPlaceholderText(/Message the team/),
    '@comrade what is left?',
  );
  await user.click(screen.getByRole('button', { name: 'SEND' }));
}

describe('a turn that cannot run', () => {
  test('a busy room says so instead of silently answering nothing', async () => {
    // 🔴 The defect D6 closes. stream_turn emits this frame and returns when
    // another member's question holds the room's turn lock (decision Q6), and
    // GroupRoom matched neither 'text' nor 'error' — so it fell through to
    // nothing. The typing indicator vanished, no reply arrived, and the
    // member's message sat in the room unanswered with no way to tell a
    // queued turn from a hung one. 'busy' was not even in StreamFrame's union,
    // which is how it stayed invisible.
    await askComrade(
      stream([
        { type: 'busy', detail: 'Comrade is working on someone else’s question in this room. Yours is next — send it again in a moment.' },
        { type: 'done', user_message_id: 'm-u', reply_message_id: null },
      ]),
    );
    expect(await screen.findByText(/Yours is next/)).toBeInTheDocument();
  });

  test('being busy is not an error', async () => {
    // Nothing failed and nothing needs retrying differently, so it must not
    // land in the send-error slot next to the composer — that reads as "your
    // message did not go through", and it did.
    await askComrade(
      stream([
        { type: 'busy', detail: 'Comrade is busy in this room.' },
        { type: 'done', user_message_id: 'm-u', reply_message_id: null },
      ]),
    );
    const note = await screen.findByText('Comrade is busy in this room.');
    expect(note).toBeInTheDocument();
    // The composer's error line is terracotta; the busy note is muted.
    expect(note).not.toHaveStyle({ color: 'var(--terracotta)' });
  });

  test('the room says what it is doing while it works', async () => {
    // The runtime emits a tool_call per tool; the room used to drop every one
    // and show three blinking dots, which is the same thing it shows when
    // nothing is happening at all.
    const { body, release } = heldStream([
      { type: 'run', run_id: 'r1' },
      { type: 'tool_call', tool: 'messages_search' },
    ]);
    await askComrade(body);
    expect(await screen.findByText(/searching the room/)).toBeInTheDocument();
    // Never the raw identifier — that was PrivateThread's old behaviour and
    // there is no reason to reproduce it here.
    expect(screen.queryByText(/messages_search/)).not.toBeInTheDocument();
    release();
  });
});

test('a deleted message renders a visible trace, never its body', async () => {
  supaState.tables.messages = [
    msg({ id: 'm-del', body: 'the secret thing', deleted_scope: 'everyone' }),
  ];
  renderInApp(<GroupRoom />);
  expect(await screen.findByText(/removed a message — removed for everyone/)).toBeInTheDocument();
  expect(screen.queryByText('the secret thing')).not.toBeInTheDocument();
});

test('an AI message carries the seen-by-all badge', async () => {
  supaState.tables.messages = [
    msg({ id: 'm-ai', sender_kind: 'ai', sender_id: null, body: 'Deadline noted.' }),
  ];
  renderInApp(<GroupRoom />);
  expect(await screen.findByText('Deadline noted.')).toBeInTheDocument();
  expect(screen.getByText('AI · SEEN BY ALL')).toBeInTheDocument();
});

test('a compilation-linked AI message renders the memory diff card', async () => {
  supaState.tables.messages = [
    msg({
      id: 'm-diff', sender_kind: 'ai', sender_id: null,
      body: 'Memory updated — 2 added, 1 revised, 0 removed.',
    }),
  ];
  supaState.tables.memory_compilations = [
    {
      id: 'comp-1', team_id: 'team-1', trigger: 'on_demand', status: 'done',
      entries_added: 2, entries_revised: 1, entries_removed: 0,
      diff_message_id: 'm-diff',
      started_at: '2026-07-20T09:00:00Z', finished_at: '2026-07-20T09:00:05Z',
    },
  ];
  renderInApp(<GroupRoom />);
  expect(await screen.findByText(/MEMORY UPDATED/)).toBeInTheDocument();
  expect(screen.getByText(/2 added · 1 revised · 0 removed/)).toBeInTheDocument();
});

describe('a plain user message', () => {
  test('renders body and sender name without any AI affordances', async () => {
    supaState.tables.messages = [msg({})];
    renderInApp(<GroupRoom />);
    expect(await screen.findByText('hello team')).toBeInTheDocument();
    expect(screen.queryByText('AI · SEEN BY ALL')).not.toBeInTheDocument();
    expect(screen.queryByText(/MEMORY UPDATED/)).not.toBeInTheDocument();
  });
});
