import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import {
  makeSupabaseMock, makeTeamMock, renderInApp, resetSupa, resetTeam, server,
} from './mocks';

vi.mock('../../src/lib/supabase', () => makeSupabaseMock());
vi.mock('../../src/state/TeamContext', () => makeTeamMock());

import { ConsentCard } from '../../src/components/ConsentCard';
import type { ConsentItem } from '../../src/lib/types';

const BASE = 'http://localhost:8000';

const item = (over: Partial<ConsentItem> = {}): ConsentItem => ({
  id: 'c1a2b3c4-0000-0000-0000-000000000001',
  team_id: 'team-1',
  requesting_member_id: 'u1',
  tool_name: 'task_create',
  tool_args: { body: 'The demo moved to Friday.' },
  source_snippet: 'can you post that the demo moved?',
  action_hash: 'sha256:abcdef1234567890',
  status: 'pending',
  reversible: true,
  tier: 'T2',
  expires_at: null,
  created_at: '2026-07-20T10:00:00Z',
  resolved_at: null,
  thread_id: 'thread-1',
  agent_run_id: null,
  resolution_reason: null,
  ...over,
});

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
beforeEach(() => {
  resetSupa();
  resetTeam();
});

describe('the warrant card shows the literal action (hard design rule)', () => {
  test('literal tool name, literal args, source snippet all visible', () => {
    renderInApp(<ConsentCard item={item()} onResolved={() => {}} viewerId="u1" />);
    expect(screen.getAllByText(/task_create/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/The demo moved to Friday\./).length).toBeGreaterThan(0);
    expect(screen.getByText(/can you post that the demo moved\?/)).toBeInTheDocument();
  });

  test('raw args JSON expands on demand', async () => {
    const user = userEvent.setup();
    renderInApp(<ConsentCard item={item()} onResolved={() => {}} viewerId="u1" />);
    await user.click(screen.getByText(/RAW ARGS — EXACT JSON/));
    expect(screen.getByText(/"body": "The demo moved to Friday\."/)).toBeInTheDocument();
  });
});

describe('approve / reject call the backend as specified', () => {
  test('only requester receives approval controls in a shared thread', () => {
    renderInApp(<ConsentCard item={item()} onResolved={() => {}} viewerId="u2" />);
    expect(screen.queryByRole('button', { name: 'ALLOW ONCE' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'ALLOW FOR THIS THREAD' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Reject' })).not.toBeInTheDocument();
    expect(screen.getByText("AWAITING REQUESTER'S KEY")).toBeInTheDocument();
  });

  test('ALLOW FOR THIS THREAD requests a bounded reusable permission', async () => {
    let seenBody: unknown = null;
    server.use(
      http.post(`${BASE}/consent/:id/approve`, async ({ request }) => {
        seenBody = await request.json();
        return HttpResponse.json({ status: 'executed', result: {} });
      }),
    );
    const user = userEvent.setup();
    renderInApp(<ConsentCard item={item()} onResolved={() => {}} viewerId="u1" />);
    await user.click(screen.getByRole('button', { name: 'ALLOW FOR THIS THREAD' }));
    await waitFor(() => expect(seenBody).toEqual({ team_id: 'team-1', grant_for_thread: true }));
  });

  test('ALLOW ONCE posts to /consent/{id}/approve with team_id', async () => {
    let seenBody: unknown = null;
    server.use(
      http.post(`${BASE}/consent/:id/approve`, async ({ request, params }) => {
        expect(params.id).toBe(item().id);
        seenBody = await request.json();
        return HttpResponse.json({ status: 'executed', result: {} });
      }),
    );
    const onResolved = vi.fn();
    const user = userEvent.setup();
    renderInApp(<ConsentCard item={item()} onResolved={onResolved} viewerId="u1" />);
    await user.click(screen.getByRole('button', { name: 'ALLOW ONCE' }));
    await waitFor(() => expect(onResolved).toHaveBeenCalled());
    expect(seenBody).toEqual({ team_id: 'team-1' });
  });

  test('Reject posts to /consent/{id}/reject', async () => {
    let called = false;
    let seenBody: unknown = null;
    server.use(
      http.post(`${BASE}/consent/:id/reject`, async ({ request }) => {
        called = true;
        seenBody = await request.json();
        return HttpResponse.json({ status: 'rejected' });
      }),
    );
    const user = userEvent.setup();
    renderInApp(<ConsentCard item={item()} onResolved={() => {}} viewerId="u1" />);
    await user.click(screen.getByRole('button', { name: 'Reject' }));
    await waitFor(() => expect(called).toBe(true));
    // no reason typed -> the field is left out, not sent as an empty string
    expect(seenBody).toEqual({ team_id: 'team-1' });
  });

  test('Reject sends the optional one-line reason when the member types one', async () => {
    let seenBody: unknown = null;
    server.use(
      http.post(`${BASE}/consent/:id/reject`, async ({ request }) => {
        seenBody = await request.json();
        return HttpResponse.json({ status: 'rejected' });
      }),
    );
    const user = userEvent.setup();
    renderInApp(<ConsentCard item={item()} onResolved={() => {}} viewerId="u1" />);
    await user.type(
      screen.getByPlaceholderText('Reason for rejecting (optional)'),
      'we already decided this in standup',
    );
    await user.click(screen.getByRole('button', { name: 'Reject' }));
    await waitFor(() =>
      expect(seenBody).toEqual({
        team_id: 'team-1',
        reason: 'we already decided this in standup',
      }),
    );
  });
});

describe('resolved cards keep their result in the timeline', () => {
  test('shows rejection reason', () => {
    renderInApp(
      <ConsentCard
        item={item({ status: 'rejected', resolution_reason: 'already covered in standup' })}
        onResolved={() => {}}
        viewerId="u1"
      />,
    );
    expect(screen.getByText(/Rejected — already covered in standup/)).toBeInTheDocument();
  });

  test('shows expiry without offering an action', () => {
    renderInApp(
      <ConsentCard
        item={item({ expires_at: '2020-01-01T00:00:00Z' })}
        onResolved={() => {}}
        viewerId="u1"
      />,
    );
    expect(screen.getByText('Expired — nothing ran.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'ALLOW ONCE' })).not.toBeInTheDocument();
  });
});

describe('stale (409) is a distinct state from resolved-elsewhere (404)', () => {
  test('409 renders the went-stale explanation and removes the action row', async () => {
    server.use(
      http.post(`${BASE}/consent/:id/approve`, () =>
        HttpResponse.json({ detail: 'consent c1 has expired' }, { status: 409 }),
      ),
    );
    const user = userEvent.setup();
    renderInApp(<ConsentCard item={item()} onResolved={() => {}} viewerId="u1" />);
    await user.click(screen.getByRole('button', { name: 'ALLOW ONCE' }));
    expect(await screen.findByText(/went stale/)).toBeInTheDocument();
    expect(screen.getByText(/STALE — NOTHING RAN/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'ALLOW ONCE' })).not.toBeInTheDocument();
  });

  test('404 renders already-resolved copy, NOT the stale copy', async () => {
    server.use(
      http.post(`${BASE}/consent/:id/approve`, () =>
        HttpResponse.json({ detail: 'not yours' }, { status: 404 }),
      ),
    );
    const user = userEvent.setup();
    renderInApp(<ConsentCard item={item()} onResolved={() => {}} viewerId="u1" />);
    await user.click(screen.getByRole('button', { name: 'ALLOW ONCE' }));
    expect(await screen.findByText(/no longer pending/)).toBeInTheDocument();
    expect(screen.queryByText(/went stale/)).not.toBeInTheDocument();
  });
});
