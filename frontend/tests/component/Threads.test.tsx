import { afterAll, afterEach, beforeAll, beforeEach, expect, test, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { http, HttpResponse } from 'msw';
import { makeSupabaseMock, makeTeamMock, renderInApp, resetSupa, resetTeam, server, supaState } from './mocks';

vi.mock('../../src/lib/supabase', () => makeSupabaseMock());
vi.mock('../../src/state/TeamContext', () => makeTeamMock());

import { Threads } from '../../src/screens/Threads';

const thread = {
  id: 'thread-1', team_id: 'team-1', title: 'Release work', visibility: 'team', kind: 'work',
  work_state: 'planned', owner_id: null, created_by: 'u1', created_at: '2026-09-04T00:00:00Z', updated_at: '2026-09-04T00:00:00Z',
};

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
