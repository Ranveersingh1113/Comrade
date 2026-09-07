import { afterAll, afterEach, beforeAll, beforeEach, expect, test, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { http, HttpResponse } from 'msw';
import { makeSupabaseMock, makeTeamMock, renderInApp, resetSupa, resetTeam, server, supaState, teamState } from './mocks';

vi.mock('../../src/lib/supabase', () => makeSupabaseMock());
vi.mock('../../src/state/TeamContext', () => makeTeamMock());

import { LegacyThreadRedirect, Threads } from '../../src/screens/Threads';

const thread = {
  id: 'thread-1', team_id: 'team-1', title: 'Release work', visibility: 'team', kind: 'work',
  work_state: 'planned', owner_id: null, created_by: 'u1', created_at: '2026-09-04T00:00:00Z', updated_at: '2026-09-04T00:00:00Z',
};

function ThreadRoute() {
  const navigate = useNavigate();
  return <><button onClick={() => navigate('/t/team-1/threads/thread-2')}>Open planning</button><Threads /></>;
}

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterAll(() => server.close());

beforeEach(() => {
  resetSupa();
  resetTeam();
});
afterEach(() => { localStorage.clear(); server.resetHandlers(); });

test('creates and opens a public thread with one click', async () => {
  const user = userEvent.setup();
  renderInApp(<Threads />);
  await user.click(await screen.findByRole('button', { name: 'New thread' }));

  await waitFor(() => expect(supaState.inserts[0]).toMatchObject({ table: 'threads' }));
  expect(supaState.inserts[0]?.values).toMatchObject({
    team_id: 'team-1', title: 'New thread', visibility: 'team', kind: 'discussion', created_by: 'u1',
  });
  expect(screen.queryByLabelText('Thread title')).toBeNull();
});

test('non-group threads are Comrade-only and send through the agent', async () => {
  let body: unknown;
  server.use(
    http.post('http://localhost:8000/agent/turn', async ({ request }) => {
      body = await request.json();
      return HttpResponse.json({ run_id: 'run-1', status: 'queued' });
    }),
    http.get('http://localhost:8000/agent/runs/run-1/stream', () =>
      new HttpResponse('{"type":"done"}\n')),
  );
  supaState.tables.threads = [thread];
  const user = userEvent.setup();
  render(<MemoryRouter initialEntries={['/t/team-1/threads/thread-1']}><Routes><Route path="/t/:teamId/threads/:threadId" element={<Threads />} /></Routes></MemoryRouter>);
  // T14: a team-visible WORK thread opens on Comrade and keeps the switch —
  // the people in it still need to talk to each other about the work. Only a
  // thread nobody else can read is Comrade-only.
  expect(await screen.findByPlaceholderText('Ask Comrade…')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Comrade mode' }))
    .toHaveAttribute('aria-pressed', 'true');
  await user.type(screen.getByPlaceholderText('Ask Comrade…'), 'check the release');
  await user.click(screen.getByRole('button', { name: 'SEND' }));
  await waitFor(() => expect(body).toMatchObject({ team_id: 'team-1', text: 'check the release', thread_id: 'thread-1' }));
});

test('shows a thread consent card inline with its messages', async () => {
  supaState.tables.threads = [thread];
  supaState.tables.messages = [{
    id: 'resumed-message', team_id: 'team-1', thread_id: 'thread-1', sender_kind: 'ai',
    sender_id: null, body: 'Comrade resumed after permission.', deleted_scope: null,
    deleted_by: null, deleted_at: null, ai_assisted: false, created_at: '2026-09-04T11:00:00Z',
  }];
  supaState.tables.consent_queue = [{
    id: 'consent-1', team_id: 'team-1', thread_id: 'thread-1', agent_run_id: null,
    requesting_member_id: 'u1', tool_name: 'task_create', tool_args: { title: 'Publish notes' },
    source_snippet: 'publish notes', action_hash: 'abc', status: 'pending', reversible: true,
    tier: 'T1', expires_at: null, created_at: '2026-09-04T10:00:00Z', resolved_at: null,
    resolution_reason: null,
  }];
  render(<MemoryRouter initialEntries={['/t/team-1/threads/thread-1']}><Routes><Route path="/t/:teamId/threads/:threadId" element={<Threads />} /></Routes></MemoryRouter>);
  const card = await screen.findByTestId('consent-card');
  expect(card).toHaveAttribute('data-consent-id', 'consent-1');
  expect(card.compareDocumentPosition(screen.getByText('Comrade resumed after permission.')))
    .toBe(Node.DOCUMENT_POSITION_FOLLOWING);
});

test('a thread nobody else can read stays Comrade-only despite saved team preferences', async () => {
  // T14 narrowed what "Comrade-only" means. It is no longer "not the General
  // thread" — a team-visible thread lets the team talk in it — it is "nobody
  // else can read this", which is a restricted thread with one person in it.
  localStorage.setItem('comrade.composerMode.u1.thread-2', 'team');
  supaState.tables.threads = [
    thread,
    { ...thread, id: 'thread-2', title: 'Planning', kind: 'discussion',
      work_state: null, visibility: 'restricted' },
  ];
  supaState.tables.thread_participants = [
    { thread_id: 'thread-2', team_id: 'team-1', user_id: 'u1', added_by: 'u1',
      joined_at: '2026-09-04T00:00:00Z' },
  ];
  const user = userEvent.setup();
  render(<MemoryRouter initialEntries={['/t/team-1/threads/thread-1']}><Routes><Route path="/t/:teamId/threads/:threadId" element={<ThreadRoute />} /></Routes></MemoryRouter>);
  expect(await screen.findByPlaceholderText('Ask Comrade…')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Open planning' }));
  expect(await screen.findByPlaceholderText('Ask Comrade…')).toBeInTheDocument();
});

test('missing legacy private thread gives the member a route back to threads', async () => {
  render(<MemoryRouter initialEntries={['/t/team-1/thread']}><Routes><Route path="/t/:teamId/thread" element={<LegacyThreadRedirect privateThread />} /></Routes></MemoryRouter>);
  expect(await screen.findByText(/No private thread exists yet/)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'View threads' })).toHaveAttribute('href', '/t/team-1/threads');
});

test('changing member identity resets the mounted thread composer and its send path', async () => {
  localStorage.setItem('comrade.composerMode.u1.thread-1', 'team');
  localStorage.setItem('comrade.composerMode.u2.thread-1', 'agent');
  supaState.tables.threads = [thread];
  let body: unknown;
  server.use(
    http.post('http://localhost:8000/agent/turn', async ({ request }) => {
      body = await request.json();
      return HttpResponse.json({ run_id: 'run-1', status: 'queued' });
    }),
    http.get('http://localhost:8000/agent/runs/run-1/stream', () =>
      new HttpResponse('{"type":"done"}\n')),
  );
  const user = userEvent.setup();
  const app = () => <MemoryRouter initialEntries={['/t/team-1/threads/thread-1']}><Routes><Route path="/t/:teamId/threads/:threadId" element={<Threads />} /></Routes></MemoryRouter>;
  const { rerender } = render(app());
  // u1 saved 'team' and the thread now allows it (T14: a team-visible work
  // thread keeps the switch), so u1 gets the team composer...
  expect(await screen.findByPlaceholderText(/Message the team/)).toBeInTheDocument();

  teamState.myUserId = 'u2';
  rerender(app());
  // ...and u2, who saved 'agent', gets Comrade. That the composer follows the
  // VIEWER is the thing this test exists for.
  expect(await screen.findByPlaceholderText('Ask Comrade…')).toBeInTheDocument();
  await user.type(screen.getByPlaceholderText('Ask Comrade…'), 'review this');
  await user.click(screen.getByRole('button', { name: 'SEND' }));
  await waitFor(() => expect(body).toMatchObject({ team_id: 'team-1', text: 'review this', thread_id: 'thread-1' }));
});
