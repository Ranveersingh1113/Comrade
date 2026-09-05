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

test('creates a selected-members thread with the selected participant', async () => {
  // A regression to `visibility: team` or omitting participant rows exposes a
  // restricted conversation to everyone; both writes are observable browser
  // contracts and RLS remains the enforcing boundary.
  const user = userEvent.setup();
  renderInApp(<Threads />);
  await user.click(await screen.findByRole('button', { name: 'New thread' }));
  await user.type(screen.getByLabelText('Thread title'), 'Release prep');
  await user.click(screen.getByLabelText('Restricted to selected members'));
  await user.click(screen.getByLabelText('Marcus Lee'));
  await user.click(screen.getByRole('button', { name: 'Create thread' }));

  await waitFor(() => expect(supaState.inserts[0]).toMatchObject({ table: 'threads' }));
  expect(supaState.inserts[0]?.values).toMatchObject({
    team_id: 'team-1', title: 'Release prep', visibility: 'restricted', kind: 'discussion', created_by: 'u1',
  });
  expect(supaState.inserts[1]).toMatchObject({ table: 'thread_participants' });
  expect(supaState.inserts[1]?.values).toMatchObject({ team_id: 'team-1', user_id: 'u1' });
  expect(supaState.inserts[2]?.values).toMatchObject({ team_id: 'team-1', user_id: 'u2' });
});

test('agent mode sends the canonical thread id', async () => {
  let body: unknown;
  server.use(http.post('http://localhost:8000/agent/turn/stream', async ({ request }) => {
    body = await request.json();
    return new HttpResponse('{"type":"done"}\n');
  }));
  supaState.tables.threads = [thread];
  const user = userEvent.setup();
  render(<MemoryRouter initialEntries={['/t/team-1/threads/thread-1']}><Routes><Route path="/t/:teamId/threads/:threadId" element={<Threads />} /></Routes></MemoryRouter>);
  await user.click(await screen.findByRole('button', { name: 'Agent mode' }));
  await user.type(screen.getByPlaceholderText('Ask Comrade…'), 'check the release');
  await user.click(screen.getByRole('button', { name: 'SEND' }));
  await waitFor(() => expect(body).toEqual({ team_id: 'team-1', text: 'check the release', thread_id: 'thread-1' }));
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

test('switching thread routes resets the visible composer mode for the new thread', async () => {
  supaState.tables.threads = [
    thread,
    { ...thread, id: 'thread-2', title: 'Planning', kind: 'discussion', work_state: null },
  ];
  const user = userEvent.setup();
  render(<MemoryRouter initialEntries={['/t/team-1/threads/thread-1']}><Routes><Route path="/t/:teamId/threads/:threadId" element={<ThreadRoute />} /></Routes></MemoryRouter>);
  expect((await screen.findByLabelText('Composer mode: agent'))).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Open planning' }));
  expect(await screen.findByLabelText('Composer mode: team')).toBeInTheDocument();
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
  server.use(http.post('http://localhost:8000/agent/turn/stream', async ({ request }) => {
    body = await request.json();
    return new HttpResponse('{"type":"done"}\n');
  }));
  const user = userEvent.setup();
  const app = () => <MemoryRouter initialEntries={['/t/team-1/threads/thread-1']}><Routes><Route path="/t/:teamId/threads/:threadId" element={<Threads />} /></Routes></MemoryRouter>;
  const { rerender } = render(app());
  expect(await screen.findByLabelText('Composer mode: team')).toBeInTheDocument();

  teamState.myUserId = 'u2';
  rerender(app());
  expect(await screen.findByLabelText('Composer mode: agent')).toBeInTheDocument();
  await user.type(screen.getByPlaceholderText('Ask Comrade…'), 'review this');
  await user.click(screen.getByRole('button', { name: 'SEND' }));
  await waitFor(() => expect(body).toEqual({ team_id: 'team-1', text: 'review this', thread_id: 'thread-1' }));
});
