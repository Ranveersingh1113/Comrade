import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { http, HttpResponse } from 'msw';
import { makeSupabaseMock, makeTeamMock, resetSupa, resetTeam, server, supaState } from './mocks';

vi.mock('../../src/lib/supabase', () => makeSupabaseMock());
vi.mock('../../src/state/TeamContext', () => makeTeamMock());

import { GitHubConnect } from '../../src/components/GitHubConnect';

const BASE = 'http://localhost:8000';
const TEAM = 'team-1';

function renderAt(path: string, isLeader = true) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <GitHubConnect teamId={TEAM} isLeader={isLeader} />
    </MemoryRouter>,
  );
}

function installLink(body: unknown) {
  return http.get(`${BASE}/teams/${TEAM}/github/install`, () => HttpResponse.json(body));
}

function repos(body: unknown) {
  return http.get(`${BASE}/teams/${TEAM}/github/repositories`, () => HttpResponse.json(body));
}

const ONE_INSTALL = {
  installations: [
    {
      installation_id: 42,
      account_login: 'acme',
      repositories: [
        { full_name: 'acme/app', connected: false, cloned_at: null, sync_error: null },
        {
          full_name: 'acme/site',
          connected: true,
          cloned_at: '2026-09-02T10:00:00Z',
          sync_error: null,
        },
      ],
    },
  ],
};

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
beforeEach(() => {
  resetSupa();
  resetTeam();
});

describe('GitHubConnect', () => {
  test('offers exactly the repositories the installation grants', async () => {
    // Not a free-text box, and that is the design rather than a convenience: a
    // repository the team has not granted cannot be reached by the token we
    // mint for them, so typing one would only produce failures that look like
    // our bug.
    server.use(installLink({ configured: true, url: 'https://github.com/apps/c/installations/new?state=s' }), repos(ONE_INSTALL));
    renderAt('/setup');

    expect(await screen.findByText('acme/app')).toBeTruthy();
    expect(screen.getByText('acme/site')).toBeTruthy();
    expect(screen.getByText('CONNECTED')).toBeTruthy();
  });

  test('connecting writes the installation id, not just the name', async () => {
    // 🔴 The row is only insertable through an installation the same team
    // owns (au_github_repos_insert). Sending the name alone would be refused
    // by RLS, and the failure would look like a permissions bug.
    server.use(installLink({ configured: true, url: 'https://x' }), repos(ONE_INSTALL));
    renderAt('/setup');
    await screen.findByText('acme/app');

    await userEvent.click(screen.getAllByRole('button', { name: 'connect' })[0]);
    await waitFor(() => expect(supaState.inserts.length).toBe(1));
    expect(supaState.inserts[0]).toEqual({
      table: 'github_repos',
      values: { team_id: TEAM, repo_full_name: 'acme/app', installation_id: 42 },
    });
  });

  test('disconnecting deletes the row', async () => {
    server.use(installLink({ configured: true, url: 'https://x' }), repos(ONE_INSTALL));
    renderAt('/setup');
    await screen.findByText('acme/site');

    await userEvent.click(screen.getByRole('button', { name: 'disconnect' }));
    await waitFor(() => expect(supaState.deletes.length).toBe(1));
    expect(supaState.deletes[0].values).toEqual({
      team_id: TEAM, repo_full_name: 'acme/site',
    });
  });

  test('a member who is not a lead can see but not change', async () => {
    server.use(installLink({ configured: true, url: 'https://x' }), repos(ONE_INSTALL));
    renderAt('/setup', false);
    await screen.findByText('acme/app');

    expect(screen.getAllByRole('button', { name: 'connect' })[0]).toHaveProperty('disabled', true);
    expect(screen.getByText(/Only a team lead/)).toBeTruthy();
  });

  test('no App configured says why instead of offering a broken link', async () => {
    // A link that cannot complete is worse than no link: it sends someone to
    // GitHub, has them install something, and fails on the way back.
    server.use(
      installLink({ configured: false, reason: 'No GitHub App is set up for this deployment.' }),
      repos({ installations: [] }),
    );
    renderAt('/setup');

    expect(await screen.findByText(/No GitHub App is set up/)).toBeTruthy();
    expect(screen.queryByRole('link')).toBeNull();
  });

  test('one broken installation does not hide the others', async () => {
    // A team that revoked one grant still needs to see and manage the rest.
    server.use(
      installLink({ configured: true, url: 'https://x' }),
      repos({
        installations: [
          { installation_id: 1, account_login: 'broken', repositories: [], error: 'GitHub refused a token' },  // eslint-disable-line
          ONE_INSTALL.installations[0],
        ],
      }),
    );
    renderAt('/setup');

    expect(await screen.findByText(/GitHub refused a token/)).toBeTruthy();
    expect(screen.getByText('acme/app')).toBeTruthy();
  });

  test('a connected repository that has not cloned yet says so', async () => {
    // 🔴 CONNECTED on its own was a lie in exactly the state people hit first.
    // Between connecting and the reconciler's next tick, this screen said
    // CONNECTED while the agent said no repository was connected.
    server.use(
      installLink({ configured: true, url: 'https://x' }),
      repos({
        installations: [{
          installation_id: 42, account_login: 'acme',
          repositories: [
            { full_name: 'acme/app', connected: true, cloned_at: null, sync_error: null },
          ],
        }],
      }),
    );
    renderAt('/setup');
    expect(await screen.findByText('CLONING…')).toBeTruthy();
    expect(screen.queryByText('CONNECTED')).toBeNull();
  });

  test('a clone that failed names the reason instead of claiming success', async () => {
    // The failure that matters most is a credential one, and it is invisible
    // from anywhere else a member can look: they have no grant on `jobs`.
    server.use(
      installLink({ configured: true, url: 'https://x' }),
      repos({
        installations: [{
          installation_id: 42, account_login: 'acme',
          repositories: [{
            full_name: 'acme/app', connected: true, cloned_at: null,
            sync_error: 'no GitHub credential reaches acme/app.',
          }],
        }],
      }),
    );
    renderAt('/setup');
    expect(await screen.findByText('COULD NOT CLONE')).toBeTruthy();
    expect(screen.getByText(/no GitHub credential reaches/)).toBeTruthy();
    expect(screen.queryByText('CONNECTED')).toBeNull();
  });

  test('a clone in flight is polled until it finishes', async () => {
    // 🔴 The screen loaded once on mount, so it said CLONING… until somebody
    // reloaded. A clone that finished in four seconds was indistinguishable
    // from one that had hung, and the honest state read as a broken one.
    let call = 0;
    server.use(
      installLink({ configured: true, url: 'https://x' }),
      http.get(`${BASE}/teams/${TEAM}/github/repositories`, () => {
        call += 1;
        const done = call > 1;
        return HttpResponse.json({
          installations: [{
            installation_id: 42, account_login: 'acme',
            repositories: [{
              full_name: 'acme/app', connected: true,
              cloned_at: done ? '2026-09-02T10:00:00Z' : null,
              sync_error: null,
            }],
          }],
        });
      }),
    );
    renderAt('/setup');
    expect(await screen.findByText('CLONING…')).toBeTruthy();
    expect(await screen.findByText('CONNECTED', {}, { timeout: 8000 })).toBeTruthy();
  }, 12000);

  test('a settled screen stops asking', async () => {
    // Polling forever would be a request every four seconds per open tab, for
    // a state that cannot change on its own.
    let calls = 0;
    server.use(
      installLink({ configured: true, url: 'https://x' }),
      http.get(`${BASE}/teams/${TEAM}/github/repositories`, () => {
        calls += 1;
        return HttpResponse.json(ONE_INSTALL);
      }),
    );
    renderAt('/setup');
    await screen.findByText('CONNECTED');
    const after = calls;
    await new Promise((r) => setTimeout(r, 5000));
    expect(calls).toBe(after);
  }, 12000);
});
