import { beforeEach, describe, expect, test, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  makeSupabaseMock, makeTeamMock, renderInApp, resetSupa, resetTeam, supaState,
} from './mocks';

vi.mock('../../src/lib/supabase', () => makeSupabaseMock());
vi.mock('../../src/state/TeamContext', () => makeTeamMock());

import { MemoryDiffCard } from '../../src/components/MemoryDiffCard';
import type { MemoryCompilation } from '../../src/lib/types';

const compilation: MemoryCompilation = {
  id: 'abcd1234-0000-0000-0000-000000000001',
  team_id: 'team-1',
  trigger: 'on_demand',
  status: 'done',
  entries_added: 2,
  entries_revised: 1,
  entries_removed: 0,
  diff_message_id: 'm-diff',
  started_at: '2026-07-20T10:00:00Z',
  finished_at: '2026-07-20T10:00:05Z',
};

beforeEach(() => {
  resetSupa();
  resetTeam();
});

test('collapsed card shows the diff counts and the wiki pointer', () => {
  renderInApp(<MemoryDiffCard compilation={compilation} />);
  expect(screen.getByText(/2 added · 1 revised · 0 removed/)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: /team wiki/ })).toBeInTheDocument();
});

test('expanding lists the compilation versions', async () => {
  supaState.tables.memory_versions = [
    {
      id: 'v1', entry_id: 'e1', team_id: 'team-1', compilation_id: compilation.id,
      fact: 'Demo is Friday', change_type: 'added', is_active: true,
      created_at: '2026-07-20T10:00:01Z',
    },
  ];
  supaState.tables.memory_reverts = [];
  const user = userEvent.setup();
  renderInApp(<MemoryDiffCard compilation={compilation} />);
  await user.click(screen.getByText(/VIEW CHANGES/));
  expect(await screen.findByText('Demo is Friday')).toBeInTheDocument();
  expect(screen.getByText('ADD')).toBeInTheDocument();
});

test('one-tap revert inserts memory_reverts as the viewer, then shows queued', async () => {
  supaState.tables.memory_versions = [
    {
      id: 'v1', entry_id: 'e1', team_id: 'team-1', compilation_id: compilation.id,
      fact: 'Demo is Friday', change_type: 'added', is_active: true,
      created_at: '2026-07-20T10:00:01Z',
    },
  ];
  supaState.tables.memory_reverts = [];
  const user = userEvent.setup();
  renderInApp(<MemoryDiffCard compilation={compilation} />);
  await user.click(screen.getByText(/VIEW CHANGES/));
  await user.click(await screen.findByRole('button', { name: /revert/ }));

  await waitFor(() => {
    expect(supaState.inserts).toHaveLength(1);
  });
  expect(supaState.inserts[0]).toEqual({
    table: 'memory_reverts',
    values: {
      entry_id: 'e1',
      team_id: 'team-1',
      member_id: 'u1', // the signed-in viewer — never someone else
      reverted_version_id: 'v1',
    },
  });
  expect(screen.getByText('REVERT QUEUED')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /revert/ })).not.toBeInTheDocument();
});

test('a failed revert surfaces the error instead of faking success', async () => {
  supaState.tables.memory_versions = [
    {
      id: 'v1', entry_id: 'e1', team_id: 'team-1', compilation_id: compilation.id,
      fact: 'Demo is Friday', change_type: 'added', is_active: true,
      created_at: '2026-07-20T10:00:01Z',
    },
  ];
  supaState.insertErrors.memory_reverts = 'row-level security violation';
  const user = userEvent.setup();
  renderInApp(<MemoryDiffCard compilation={compilation} />);
  await user.click(screen.getByText(/VIEW CHANGES/));
  await user.click(await screen.findByRole('button', { name: /revert/ }));
  expect(await screen.findByText(/row-level security violation/)).toBeInTheDocument();
  expect(screen.queryByText('REVERT QUEUED')).not.toBeInTheDocument();
});
