import { beforeEach, describe, expect, test, vi } from 'vitest';
import { screen } from '@testing-library/react';
import {
  makeSupabaseMock, makeTeamMock, renderInApp, resetSupa, resetTeam, supaState,
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

beforeEach(() => {
  resetSupa();
  resetTeam();
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
