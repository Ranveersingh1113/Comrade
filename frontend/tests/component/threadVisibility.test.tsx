import {
  afterAll, afterEach, beforeAll, beforeEach, describe, expect, test, vi,
} from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { render } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import {
  makeSupabaseMock, makeTeamMock, resetSupa, resetTeam, server, supaState,
} from './mocks';

vi.mock('../../src/lib/supabase', () => makeSupabaseMock());
vi.mock('../../src/state/TeamContext', () => makeTeamMock());

import { Threads } from '../../src/screens/Threads';

/**
 * 🔴 THE DEFECT. Team discussion was gated on the thread being TITLED
 * "General":
 *
 *     const isCanonicalGroupRoom = active?.title === 'General' && ...
 *
 * So every other public thread was Comrade-only — a team could open a thread
 * called "Release planning", visible to everyone, and find they could not talk
 * to each other in it. And renaming General silently removed team chat from
 * the one room that had it.
 */

const BASE = 'http://localhost:8000';

const thread = (over: Record<string, unknown>) => ({
  id: 'thread-1', team_id: 'team-1', title: 'General', visibility: 'team',
  kind: 'discussion', work_state: null, owner_id: null, created_by: 'u1',
  created_at: '2026-07-20T09:00:00Z', updated_at: '2026-07-20T09:00:00Z',
  ...over,
});

const openThread = (id: string) => render(
  <MemoryRouter initialEntries={[`/t/team-1/threads/${id}`]}>
    <Routes>
      <Route path="/t/:teamId/threads/:threadId" element={<Threads />} />
    </Routes>
  </MemoryRouter>,
);

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
beforeEach(() => {
  localStorage.clear();
  resetSupa();
  resetTeam();
  server.use(http.get(`${BASE}/threads/:id/agent-runs`, () => HttpResponse.json([])));
});
afterAll(() => server.close());

describe('who may talk in a thread', () => {
  test('any team-visible discussion allows team messages, not only General', async () => {
    supaState.tables.threads = [thread({ id: 'thread-9', title: 'Release planning' })];

    openThread('thread-9');

    expect(await screen.findByPlaceholderText(/Message the team/)).toBeInTheDocument();
  });

  test('a team-visible work thread keeps the switch but starts on Comrade', async () => {
    // Work threads are mostly Comrade's; the team can still say something.
    supaState.tables.threads = [
      thread({ id: 'thread-w', title: 'Fix the migration', kind: 'work', work_state: 'active' }),
    ];

    openThread('thread-w');

    expect(await screen.findByPlaceholderText('Ask Comrade…')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Comrade mode' }))
      .toHaveAttribute('aria-pressed', 'true');
  });

  test('a private thread is Comrade-only and offers no switch', async () => {
    supaState.tables.threads = [
      thread({ id: 'thread-p', title: 'Sketches', visibility: 'restricted' }),
    ];
    supaState.tables.thread_participants = [
      { thread_id: 'thread-p', team_id: 'team-1', user_id: 'u1', added_by: 'u1',
        joined_at: '2026-07-20T09:00:00Z' },
    ];

    openThread('thread-p');

    expect(await screen.findByPlaceholderText('Ask Comrade…')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Comrade mode' })).toBeNull();
  });

  test('a restricted thread with several people is a conversation between them', async () => {
    supaState.tables.threads = [
      thread({ id: 'thread-s', title: 'Design review', visibility: 'restricted' }),
    ];
    supaState.tables.thread_participants = [
      { thread_id: 'thread-s', team_id: 'team-1', user_id: 'u1', added_by: 'u1',
        joined_at: '2026-07-20T09:00:00Z' },
      { thread_id: 'thread-s', team_id: 'team-1', user_id: 'u2', added_by: 'u1',
        joined_at: '2026-07-20T09:00:00Z' },
    ];

    openThread('thread-s');

    expect(await screen.findByPlaceholderText(/Message the team/)).toBeInTheDocument();
  });
});

describe('the thread list', () => {
  test('labels a one-person restricted thread Private', async () => {
    supaState.tables.threads = [
      thread({ id: 'thread-p', title: 'Sketches', visibility: 'restricted' }),
    ];
    supaState.tables.thread_participants = [
      { thread_id: 'thread-p', team_id: 'team-1', user_id: 'u1', added_by: 'u1',
        joined_at: '2026-07-20T09:00:00Z' },
    ];

    render(
      <MemoryRouter initialEntries={['/t/team-1/threads']}>
        <Routes><Route path="/t/:teamId/threads" element={<Threads />} /></Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText(/Private/)).toBeInTheDocument();
    expect(screen.queryByText(/Selected members/)).toBeNull();
  });

  test('labels a shared restricted thread Selected members', async () => {
    supaState.tables.threads = [
      thread({ id: 'thread-s', title: 'Design review', visibility: 'restricted' }),
    ];
    supaState.tables.thread_participants = [
      { thread_id: 'thread-s', team_id: 'team-1', user_id: 'u1', added_by: 'u1',
        joined_at: '2026-07-20T09:00:00Z' },
      { thread_id: 'thread-s', team_id: 'team-1', user_id: 'u2', added_by: 'u1',
        joined_at: '2026-07-20T09:00:00Z' },
    ];

    render(
      <MemoryRouter initialEntries={['/t/team-1/threads']}>
        <Routes><Route path="/t/:teamId/threads" element={<Threads />} /></Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText(/Selected members/)).toBeInTheDocument();
  });

  test('a failed refresh keeps the list that is already on screen', async () => {
    // 🔴 Showing an empty list because one request failed tells the member
    // their threads are gone.
    supaState.tables.threads = [thread({ id: 'thread-1' })];

    render(
      <MemoryRouter initialEntries={['/t/team-1/threads']}>
        <Routes><Route path="/t/:teamId/threads" element={<Threads />} /></Routes>
      </MemoryRouter>,
    );
    await screen.findByText('General');

    supaState.failNextSelect = 'threads is unavailable';
    window.dispatchEvent(new Event('focus'));

    await waitFor(() => expect(screen.getByText('General')).toBeInTheDocument());
  });

  test('a new thread can be created for selected members', async () => {
    supaState.tables.threads = [];
    const user = userEvent.setup();

    render(
      <MemoryRouter initialEntries={['/t/team-1/threads']}>
        <Routes><Route path="/t/:teamId/threads" element={<Threads />} /></Routes>
      </MemoryRouter>,
    );
    await user.click(await screen.findByRole('button', { name: 'New private thread' }));
    await user.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => expect(supaState.inserts).toContainEqual(
      expect.objectContaining({
        table: 'threads',
        values: expect.objectContaining({ visibility: 'restricted' }),
      }),
    ));
  });
});

describe('managing who is in a restricted thread', () => {
  const seat = (thread_id: string, user_id: string, added_by = 'u1') => ({
    thread_id, team_id: 'team-1', user_id, added_by,
    joined_at: '2026-07-20T09:00:00Z',
  });

  const openRoster = (createdBy = 'u1') => {
    supaState.tables.threads = [thread({
      id: 'thread-r', title: 'Design review', visibility: 'restricted',
      created_by: createdBy,
    })];
    // One seat taken, so somebody on the team is still addable — otherwise an
    // absent Add control proves nothing about who is allowed to use it.
    supaState.tables.thread_participants = [seat('thread-r', 'u1', createdBy)];
    openThread('thread-r');
  };

  test('the people who can read the thread are shown', async () => {
    // Nothing showed the people in a restricted thread who else could read
    // what they wrote.
    openRoster();

    await waitFor(() => expect(
      document.querySelector('[data-thread-roster]')?.textContent,
    ).toMatch(/In this thread \(1\)/));
  });

  test('adding someone warns that they get the whole history first', async () => {
    // 🔴 Nothing said so. Adding a person to a thread does not start their
    // view at today — they get every message, file and Comrade run in it,
    // from the beginning — and the person adding them is thinking about the
    // next message, not the last three months of them.
    const user = userEvent.setup();
    openRoster();

    await user.click(await screen.findByRole('button', { name: 'Add someone' }));

    expect(document.querySelector('[data-history-warning]')?.textContent)
      .toMatch(/entire history/i);
  });

  test('only the thread’s creator is offered the add control', async () => {
    // RLS refuses anyone else, and offering a button that will be refused is
    // worse than not offering it.
    openRoster('someone-else');

    await waitFor(() =>
      expect(document.querySelector('[data-thread-roster]')).not.toBeNull());
    expect(screen.queryByRole('button', { name: 'Add someone' })).toBeNull();
  });
});
