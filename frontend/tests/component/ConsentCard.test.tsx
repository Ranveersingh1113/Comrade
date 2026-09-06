import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { makeSupabaseMock, makeTeamMock, renderInApp, resetSupa, resetTeam, server } from './mocks';

vi.mock('../../src/lib/supabase', () => makeSupabaseMock());
vi.mock('../../src/state/TeamContext', () => makeTeamMock());

import { ConsentCard } from '../../src/components/ConsentCard';
import type { ConsentItem } from '../../src/lib/types';

const BASE = 'http://localhost:8000';
const item = (over: Partial<ConsentItem> = {}): ConsentItem => ({
  id: 'c1a2b3c4-0000-0000-0000-000000000001', team_id: 'team-1', requesting_member_id: 'u1',
  tool_name: 'task_create', tool_args: { title: 'Publish notes', description: 'The demo moved to Friday.' },
  source_snippet: 'can you post that the demo moved?', action_hash: 'sha256:abcdef1234567890',
  status: 'pending', reversible: true, tier: 'T2', expires_at: null,
  created_at: '2026-07-20T10:00:00Z', resolved_at: null, thread_id: 'thread-1',
  agent_run_id: null, resolution_reason: null, ...over,
});

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterAll(() => server.close());
afterEach(() => server.resetHandlers());
beforeEach(() => { resetSupa(); resetTeam(); });

describe('approval summary', () => {
  test('explains the action without exposing tool internals', () => {
    renderInApp(<ConsentCard item={item()} onResolved={() => {}} viewerId="u1" />);
    expect(screen.getByText('Create “Publish notes”')).toBeInTheDocument();
    expect(screen.getByText(/The demo moved to Friday/)).toBeInTheDocument();
    expect(screen.getByText(/can you post that/)).toBeInTheDocument();
    expect(screen.getByText('AWAITING YOUR APPROVAL')).toBeInTheDocument();
    expect(screen.queryByText(/task_create/)).not.toBeInTheDocument();
    expect(screen.queryByText(/abcdef1234567890/)).not.toBeInTheDocument();
    expect(screen.queryByText(/RAW ARGS/)).not.toBeInTheDocument();
  });

  test('shows requester waiting copy to other members', () => {
    renderInApp(<ConsentCard item={item()} onResolved={() => {}} viewerId="u2" />);
    expect(screen.getByText('AWAITING REQUESTER APPROVAL')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Approve once' })).not.toBeInTheDocument();
  });
});

test('approval and thread approval preserve backend calls', async () => {
  const bodies: unknown[] = [];
  server.use(http.post(`${BASE}/consent/:id/approve`, async ({ request }) => {
    bodies.push(await request.json());
    return HttpResponse.json({ status: 'executed' });
  }));
  const user = userEvent.setup();
  const { rerender } = renderInApp(<ConsentCard item={item()} onResolved={() => {}} viewerId="u1" />);
  await user.click(screen.getByRole('button', { name: 'Approve once' }));
  await waitFor(() => expect(bodies).toContainEqual({ team_id: 'team-1' }));
  rerender(<ConsentCard item={item({ id: 'c2' })} onResolved={() => {}} viewerId="u1" />);
  await user.click(screen.getByRole('button', { name: 'Approve for this thread' }));
  await waitFor(() => expect(bodies).toContainEqual({ team_id: 'team-1', grant_for_thread: true }));
});

test('reject preserves the optional reason backend call', async () => {
  let body: unknown;
  server.use(http.post(`${BASE}/consent/:id/reject`, async ({ request }) => {
    body = await request.json();
    return HttpResponse.json({ status: 'rejected' });
  }));
  const user = userEvent.setup();
  renderInApp(<ConsentCard item={item()} onResolved={() => {}} viewerId="u1" />);
  await user.type(screen.getByPlaceholderText('Reason for declining (optional)'), 'already covered');
  await user.click(screen.getByRole('button', { name: 'Reject' }));
  await waitFor(() => expect(body).toEqual({ team_id: 'team-1', reason: 'already covered' }));
});