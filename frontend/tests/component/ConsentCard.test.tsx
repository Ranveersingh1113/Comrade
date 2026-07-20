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
  tool_name: 'post_group_message',
  tool_args: { body: 'The demo moved to Friday.' },
  source_snippet: 'can you post that the demo moved?',
  action_hash: 'sha256:abcdef1234567890',
  status: 'pending',
  reversible: true,
  tier: 'T2',
  second_approver_id: null,
  second_approved_at: null,
  expires_at: null,
  created_at: '2026-07-20T10:00:00Z',
  resolved_at: null,
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
    expect(screen.getAllByText(/post_group_message/).length).toBeGreaterThan(0);
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
  test('APPROVE posts to /consent/{id}/approve with team_id', async () => {
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
    await user.click(screen.getByRole('button', { name: 'APPROVE' }));
    await waitFor(() => expect(onResolved).toHaveBeenCalled());
    expect(seenBody).toEqual({ team_id: 'team-1' });
  });

  test('Reject posts to /consent/{id}/reject', async () => {
    let called = false;
    server.use(
      http.post(`${BASE}/consent/:id/reject`, () => {
        called = true;
        return HttpResponse.json({ status: 'rejected' });
      }),
    );
    const user = userEvent.setup();
    renderInApp(<ConsentCard item={item()} onResolved={() => {}} viewerId="u1" />);
    await user.click(screen.getByRole('button', { name: 'Reject' }));
    await waitFor(() => expect(called).toBe(true));
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
    await user.click(screen.getByRole('button', { name: 'APPROVE' }));
    expect(await screen.findByText(/went stale/)).toBeInTheDocument();
    expect(screen.getByText(/STALE — NOTHING RAN/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'APPROVE' })).not.toBeInTheDocument();
  });

  test('404 renders already-resolved copy, NOT the stale copy', async () => {
    server.use(
      http.post(`${BASE}/consent/:id/approve`, () =>
        HttpResponse.json({ detail: 'not yours' }, { status: 404 }),
      ),
    );
    const user = userEvent.setup();
    renderInApp(<ConsentCard item={item()} onResolved={() => {}} viewerId="u1" />);
    await user.click(screen.getByRole('button', { name: 'APPROVE' }));
    expect(await screen.findByText(/no longer pending/)).toBeInTheDocument();
    expect(screen.queryByText(/went stale/)).not.toBeInTheDocument();
  });
});

describe('T3 two-key states', () => {
  test('a teammate sees the countersign affordance, not approve', () => {
    renderInApp(
      <ConsentCard item={item({ tier: 'T3' })} onResolved={() => {}} viewerId="u2" />,
    );
    expect(
      screen.getByRole('button', { name: /COUNTERSIGN — SECOND KEY/ }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'APPROVE' })).not.toBeInTheDocument();
  });

  test('countersign posts to /consent/{id}/second_key', async () => {
    let called = false;
    server.use(
      http.post(`${BASE}/consent/:id/second_key`, () => {
        called = true;
        return HttpResponse.json({ status: 'countersigned' });
      }),
    );
    const user = userEvent.setup();
    renderInApp(
      <ConsentCard item={item({ tier: 'T3' })} onResolved={() => {}} viewerId="u2" />,
    );
    await user.click(screen.getByRole('button', { name: /COUNTERSIGN/ }));
    await waitFor(() => expect(called).toBe(true));
  });

  test('requester sees awaiting-second-key copy after approving', () => {
    renderInApp(
      <ConsentCard
        item={item({ tier: 'T3', status: 'approved' })}
        onResolved={() => {}}
        viewerId="u1"
      />,
    );
    expect(screen.getByText(/waiting on any other member/)).toBeInTheDocument();
  });

  test('the requester of a T3 item is never offered the countersign', () => {
    renderInApp(
      <ConsentCard item={item({ tier: 'T3' })} onResolved={() => {}} viewerId="u1" />,
    );
    expect(screen.queryByRole('button', { name: /COUNTERSIGN/ })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'APPROVE' })).toBeInTheDocument();
  });
});
