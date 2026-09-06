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

const msg = (over: Record<string, unknown>) => ({
  id: 'm-1', team_id: 'team-1', thread_id: 'thread-1',
  sender_kind: 'user', sender_id: 'u1', body: 'hello team',
  deleted_scope: null, deleted_by: null, deleted_at: null,
  created_at: '2026-07-20T09:00:00Z',
  ...over,
});

const BASE = 'http://localhost:8000';
const thread = {
  id: 'thread-1', team_id: 'team-1', title: 'General', visibility: 'team' as const,
  kind: 'discussion' as const, work_state: null, owner_id: null, created_by: 'u1',
  created_at: '2026-07-20T09:00:00Z', updated_at: '2026-07-20T09:00:00Z',
};

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
beforeEach(() => {
  resetSupa();
  resetTeam();
  server.use(http.get(`${BASE}/threads/thread-1/agent-runs`, () => HttpResponse.json([])));
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
  renderInApp(<GroupRoom thread={thread} />);
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

  test('an agent action expands to its saved arguments and diff', async () => {
    const { body, release } = heldStream([
      { type: 'run', run_id: 'r1' },
      {
        type: 'tool_call', seq: 0, tool: 'repo_open_pr',
        args: { patch: 'diff --git a/readme.md b/readme.md\n+--- a/readme.md\n++++ b/readme.md\n+@@\n+-old\n++new' },
      },
    ]);
    await askComrade(body);
    const expand = await screen.findByRole('button', { name: /details for using repo_open_pr/i });
    await userEvent.click(expand);
    expect(screen.getAllByText('readme.md', { exact: false }).length).toBeGreaterThan(0);
    release();
  });
});

test('a deleted message renders a visible trace, never its body', async () => {
  supaState.tables.messages = [
    msg({ id: 'm-del', body: 'the secret thing', deleted_scope: 'everyone' }),
  ];
  renderInApp(<GroupRoom thread={thread} />);
  expect(await screen.findByText(/removed a message — removed for everyone/)).toBeInTheDocument();
  expect(screen.queryByText('the secret thing')).not.toBeInTheDocument();
});

test('an AI message carries the seen-by-all badge', async () => {
  supaState.tables.messages = [
    msg({ id: 'm-ai', sender_kind: 'ai', sender_id: null, body: 'Deadline noted.' }),
  ];
  renderInApp(<GroupRoom thread={thread} />);
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
  renderInApp(<GroupRoom thread={thread} />);
  expect(await screen.findByText(/MEMORY UPDATED/)).toBeInTheDocument();
  expect(screen.getByText(/2 added · 1 revised · 0 removed/)).toBeInTheDocument();
});

describe('a plain user message', () => {
  test('renders body and sender name without any AI affordances', async () => {
    supaState.tables.messages = [msg({})];
    renderInApp(<GroupRoom thread={thread} />);
    expect(await screen.findByText('hello team')).toBeInTheDocument();
    expect(screen.queryByText('AI · SEEN BY ALL')).not.toBeInTheDocument();
    expect(screen.queryByText(/MEMORY UPDATED/)).not.toBeInTheDocument();
  });
});


describe('remember this', () => {
  test('offers to remember a human message but never an AI one', async () => {
    // The agent's own output is not a source. Compiling it would let memory
    // cite itself — a fact whose provenance is a sentence the model produced
    // from the wiki it is about to be written into.
    supaState.tables.messages = [
      msg({ id: 'm-human', body: 'the expiry is 90 minutes' }),
      msg({ id: 'm-ai', sender_kind: 'ai', sender_id: null, body: 'Noted.' }),
    ];
    const user = userEvent.setup();
    renderInApp(<GroupRoom thread={thread} />);
    await user.hover(await screen.findByText('the expiry is 90 minutes'));
    expect(await screen.findByText(/remember this/)).toBeInTheDocument();

    // Hovering the AI message offers nothing — and the human message's own
    // offer goes away with the hover, which is why this counts zero rather
    // than one.
    await user.hover(screen.getByText('Noted.'));
    expect(screen.queryAllByText(/remember this/)).toHaveLength(0);
  });

  test('says it was queued, not that it was remembered', async () => {
    // The compiler is the only writer of memory and it still decides whether
    // there is a durable fact in there. "Remembered" would promise something
    // this cannot deliver.
    let hit = '';
    server.use(
      http.post(`${BASE}/teams/:teamId/messages/:messageId/remember`, ({ params }) => {
        hit = String(params.messageId);
        return HttpResponse.json({ job_id: 'job-1' });
      }),
    );
    supaState.tables.messages = [msg({ id: 'm-human', body: 'the expiry is 90 minutes' })];
    const user = userEvent.setup();
    renderInApp(<GroupRoom thread={thread} />);
    const row = await screen.findByText('the expiry is 90 minutes');
    await user.hover(row);
    // fireEvent, not userEvent: the button only exists while the row is
    // hovered, and userEvent's click moves the pointer first — which drops the
    // hover and unmounts the button before the click lands. Verified both ways;
    // user.click never reaches the handler here.
    fireEvent.click(screen.getByTitle(/Queue this for the wiki/));

    await waitFor(() => expect(hit).toBe('m-human'));
    expect(await screen.findByText(/Queued/)).toBeInTheDocument();
    expect(screen.queryByText(/^Remembered/)).not.toBeInTheDocument();
  });
});


test('a published draft is marked without losing the member as its author', async () => {
  // §13.4's whole shape in one assertion: the member's name is on it and the
  // provenance sits beside that, not instead of it. A message attributed to
  // Comrade would let someone disown work published under their own name.
  supaState.tables.messages = [
    msg({ id: 'm-pub', body: 'Summary, trimmed.', ai_assisted: true }),
  ];
  renderInApp(<GroupRoom thread={thread} />);
  expect(await screen.findByText('Summary, trimmed.')).toBeInTheDocument();
  expect(screen.getByText(/drafted with Comrade/)).toBeInTheDocument();
  expect(screen.queryByText('AI · SEEN BY ALL')).not.toBeInTheDocument();
});

test('an ordinary message carries no provenance marker', async () => {
  supaState.tables.messages = [msg({ id: 'm-plain', body: 'morning all' })];
  renderInApp(<GroupRoom thread={thread} />);
  await screen.findByText('morning all');
  expect(screen.queryByText(/drafted with Comrade/)).not.toBeInTheDocument();
});

test('an AI reply exposes its sender for browser journeys', async () => {
  supaState.tables.messages = [
    msg({ id: 'm-ai', sender_kind: 'ai', sender_id: null, body: 'Two tasks are open.' }),
  ];
  renderInApp(<GroupRoom thread={thread} />);

  const row = (await screen.findByText('Two tasks are open.')).closest('[data-sender]');
  expect(row).toHaveAttribute('data-sender', 'ai');
});

describe('a turn that produced nothing', () => {
  test('the terminal run frame explains the silence', async () => {
    // 🔴 The durable queue moved the agent out of the request, so the browser
    // now replays the RUN ROW (server/app.py:_run_frames) rather than draining
    // the runtime generator. The 'empty' frame the room still listens for is
    // no longer sent to it; what arrives is a terminal 'done' carrying the
    // reason. The room dropped that detail on the floor, which put the blank
    // screen back — the member watched the indicator stop and got nothing.
    await askComrade(
      stream([
        { type: 'run', run_id: 'r-1', status: 'queued' },
        {
          type: 'done', run_id: 'r-1', status: 'failed',
          detail: 'Comrade had nothing to say that time — the model came back'
            + ' empty. Nothing was changed. Try asking again.',
        },
      ]),
    );
    expect(await screen.findByText(/came back empty/)).toBeInTheDocument();
  });

  test('a turn that answered says nothing extra', async () => {
    // The same frame ends every successful turn, so the note has to be gated
    // on there BEING a reason. Gating on the terminal frame alone would hang a
    // note under every reply Comrade ever gives.
    await askComrade(
      stream([
        { type: 'run', run_id: 'r-2', status: 'queued' },
        { type: 'text', text: 'Two tasks are open.' },
        { type: 'done', run_id: 'r-2', status: 'done', detail: null },
      ]),
    );
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'SEND' })).toBeEnabled(),
    );
    expect(screen.queryByText(/came back empty/)).not.toBeInTheDocument();
  });
});
