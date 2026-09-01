/**
 * The members' half of the wiki (findings §6.3-6).
 *
 * The database tests cover who may write and delete a comment. What only
 * exists here is the rule a member actually sees: your own comment offers you
 * a way to withdraw it, and a teammate's does not. An objection somebody else
 * can erase is not an objection, and the button is where that promise is
 * either kept or quietly broken.
 */
import { beforeEach, describe, expect, test, vi } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  makeSupabaseMock, makeTeamMock, renderInApp, resetSupa, resetTeam, supaState,
} from './mocks';

vi.mock('../../src/lib/supabase', () => makeSupabaseMock());
vi.mock('../../src/state/TeamContext', () => makeTeamMock());

import { Wiki } from '../../src/screens/Wiki';

const ENTRY = 'entry-1';

function seedWiki() {
  supaState.tables.memory_pages = [
    { id: 'p1', team_id: 'team-1', title: 'Deadlines', description: '',
      kind: 'fact', created_at: '', updated_at: '' },
  ];
  supaState.tables.memory_entries = [
    { id: ENTRY, team_id: 'team-1', page_id: 'p1', archived: false },
  ];
  supaState.tables.memory_versions = [
    { id: 'v1', entry_id: ENTRY, team_id: 'team-1', fact: 'the demo is on 14 march',
      change_type: 'added', is_active: true, valid_from: '2026-07-20T09:00:00Z',
      valid_until: null, compilation_id: null, created_at: '2026-07-20T09:00:00Z' },
  ];
  supaState.tables.memory_reverts = [];
  supaState.tables.memory_citations = [];
  supaState.tables.memory_compilations = [];
}

beforeEach(() => {
  resetSupa();
  resetTeam();
  seedWiki();
});

describe('comments on a fact', () => {
  test('shows a teammate objection with no way to erase it', async () => {
    // u2 is not the viewer. §6.3-6 is about the losing side staying visible,
    // and a majority that can delete the minority's note recreates the exact
    // collapse this feature exists to prevent, one level up.
    supaState.tables.memory_comments = [
      { id: 'c1', entry_id: ENTRY, team_id: 'team-1', author_id: 'u2',
        body: 'this was true in May, not now', created_at: '2026-07-21T09:00:00Z' },
    ];
    renderInApp(<Wiki />);
    expect(await screen.findByText('this was true in May, not now')).toBeInTheDocument();
    expect(screen.queryByTitle(/Withdraw your comment/)).not.toBeInTheDocument();
  });

  test('offers to withdraw your own', async () => {
    supaState.tables.memory_comments = [
      { id: 'c1', entry_id: ENTRY, team_id: 'team-1', author_id: 'u1',
        body: 'I put this in and I was wrong', created_at: '2026-07-21T09:00:00Z' },
    ];
    renderInApp(<Wiki />);
    expect(await screen.findByText('I put this in and I was wrong')).toBeInTheDocument();
    expect(screen.getByTitle(/Withdraw your comment/)).toBeInTheDocument();
  });

  test('a comment is written against the ENTRY, not the version', async () => {
    // The design decision the whole feature turns on. Versions are replaced by
    // consolidation routinely; a comment carrying a version_id would detach
    // from the fact it argues with the moment that fact is revised.
    supaState.tables.memory_comments = [];
    const user = userEvent.setup();
    renderInApp(<Wiki />);
    await user.type(
      await screen.findByPlaceholderText(/Disagree, or add what the wiki left out/),
      'the date moved{Enter}',
    );
    const write = supaState.inserts.find((w) => w.table === 'memory_comments');
    expect(write).toBeDefined();
    expect(write!.values).toMatchObject({
      entry_id: ENTRY,
      author_id: 'u1',
      body: 'the date moved',
    });
    expect(write!.values).not.toHaveProperty('version_id');
  });

  test('an empty comment is not sent', async () => {
    // The DB refuses a blank body; the screen should not make the round trip
    // to find that out, and an Enter on an untouched field is the commonest
    // way to hit it.
    supaState.tables.memory_comments = [];
    const user = userEvent.setup();
    renderInApp(<Wiki />);
    await user.type(
      await screen.findByPlaceholderText(/Disagree, or add what the wiki left out/),
      '   {Enter}',
    );
    expect(supaState.inserts.find((w) => w.table === 'memory_comments')).toBeUndefined();
  });
});
