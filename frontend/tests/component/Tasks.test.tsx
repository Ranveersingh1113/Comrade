import { beforeEach, describe, expect, test, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  makeSupabaseMock, makeTeamMock, renderInApp, resetSupa, resetTeam, supaState, teamState,
} from './mocks';

vi.mock('../../src/lib/supabase', () => makeSupabaseMock());
vi.mock('../../src/state/TeamContext', () => makeTeamMock());

import { Tasks } from '../../src/screens/Tasks';

const task = (over: Record<string, unknown>) => ({
  id: 't-1', team_id: 'team-1', assignee_id: 'u1', title: 'Write the API docs',
  description: null, deadline: null, status: 'proposed', created_by_kind: 'user',
  created_by_id: 'u2', confirmed_at: null,
  created_at: '2026-07-20T09:00:00Z', updated_at: '2026-07-20T09:00:00Z',
  ...over,
});

beforeEach(() => {
  resetSupa();
  resetTeam();
});

describe('assignee-confirm mirror of the DB trigger', () => {
  test('the assignee sees CONFIRM on a proposed task', async () => {
    supaState.tables.tasks = [task({ assignee_id: 'u1' })];
    teamState.myUserId = 'u1';
    renderInApp(<Tasks />);
    expect(
      await screen.findByRole('button', { name: /CONFIRM — IT'S YOURS/ }),
    ).toBeInTheDocument();
  });

  test('a non-assignee sees waiting copy and NO button', async () => {
    supaState.tables.tasks = [task({ assignee_id: 'u2' })];
    teamState.myUserId = 'u1';
    renderInApp(<Tasks />);
    expect(await screen.findByText(/waiting on Marcus to confirm/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /CONFIRM/ })).not.toBeInTheDocument();
  });

  test('confirming updates the task with a confirmed_at stamp', async () => {
    supaState.tables.tasks = [task({ assignee_id: 'u1' })];
    teamState.myUserId = 'u1';
    const user = userEvent.setup();
    renderInApp(<Tasks />);
    await user.click(await screen.findByRole('button', { name: /CONFIRM — IT'S YOURS/ }));
    await waitFor(() => expect(supaState.updates).toHaveLength(1));
    const patch = supaState.updates[0].values as Record<string, unknown>;
    expect(supaState.updates[0].table).toBe('tasks');
    expect(patch.status).toBe('confirmed');
    expect(patch.confirmed_at).toBeTruthy();
  });

  test('in-progress task offers MARK DONE to its assignee only', async () => {
    supaState.tables.tasks = [task({ assignee_id: 'u2', status: 'in_progress' })];
    teamState.myUserId = 'u1';
    renderInApp(<Tasks />);
    // u1 is not the assignee: no button, and no waiting copy either (only proposed waits)
    await screen.findByText('Write the API docs');
    expect(screen.queryByRole('button', { name: /MARK DONE/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/waiting on/)).not.toBeInTheDocument();
  });
});

test('proposing a task inserts as proposed with the creator recorded', async () => {
  supaState.tables.tasks = [];
  const user = userEvent.setup();
  renderInApp(<Tasks />);
  await user.type(
    screen.getByPlaceholderText(/anyone can propose/), 'Ship the pilot deck',
  );
  await user.click(screen.getByRole('button', { name: 'PROPOSE' }));
  await waitFor(() => expect(supaState.inserts).toHaveLength(1));
  expect(supaState.inserts[0].table).toBe('tasks');
  expect(supaState.inserts[0].values).toMatchObject({
    title: 'Ship the pilot deck',
    status: 'proposed',
    created_by_kind: 'user',
    created_by_id: 'u1',
  });
});
