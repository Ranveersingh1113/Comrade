import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test, vi } from 'vitest';
import { screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import {
  makeSupabaseMock, makeTeamMock, renderInApp, resetSupa, resetTeam, server,
} from './mocks';

vi.mock('../../src/lib/supabase', () => makeSupabaseMock());
vi.mock('../../src/state/TeamContext', () => makeTeamMock());

import { Setup } from '../../src/screens/Setup';

const BASE = 'http://localhost:8000';
beforeAll(() => server.listen({ onUnhandledRequest: 'bypass' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
beforeEach(() => {
  resetSupa();
  resetTeam();
  server.use(
    http.get(`${BASE}/teams/:team/github/install`, () =>
      HttpResponse.json({ configured: false, reason: 'not set up' })),
    http.get(`${BASE}/teams/:team/github/repositories`, () =>
      HttpResponse.json({ installations: [] })),
  );
});

describe('Setup', () => {
  test('tells a team that its own AGENTS.md steers Comrade', async () => {
    // 🔴 A capability nobody knows about is indistinguishable from one that
    // does not exist. repo_guide() has read AGENTS.md / CLAUDE.md /
    // .cursorrules out of a team's repository since the repo tools shipped,
    // and no screen, document or setup step ever mentioned it — so the
    // strongest lever a team had over Comrade's behaviour on their code was
    // invisible to them.
    renderInApp(<Setup />);
    expect(await screen.findByText(/AGENTS\.md/)).toBeTruthy();
    expect(screen.getByText(/CLAUDE\.md/)).toBeTruthy();
  });

  test('and is clear that the file cannot override the consent rules', async () => {
    // A repository that accepts pull requests accepts them from strangers.
    // Saying "Comrade follows this file" without that caveat would invite a
    // team to believe more about it than is true.
    renderInApp(<Setup />);
    expect(await screen.findByText(/can.t override the consent rules/i)).toBeTruthy();
  });
});
