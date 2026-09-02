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
        { full_name: 'acme/app', connected: false },
        { full_name: 'acme/site', connected: true },
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

  test('returning from GitHub records the installation and clears the URL', async () => {
    // 🔴 All three parameters are sent. installation_id alone is an
    // unauthenticated number in a URL and installation ids are sequential, so
    // the server needs `state` and `code` to establish who this is.
    let received: unknown = null;
    server.use(
      http.post(`${BASE}/teams/${TEAM}/github/installations`, async ({ request }) => {
        received = await request.json();
        return HttpResponse.json({ team_id: TEAM, installation_id: 42, account_login: 'acme' });
      }),
      installLink({ configured: true, url: 'https://x' }),
      repos(ONE_INSTALL),
    );
    renderAt('/setup?installation_id=42&state=st&code=cd&setup_action=install');

    await waitFor(() => expect(received).toEqual({
      installation_id: 42, state: 'st', code: 'cd',
    }));
    expect(await screen.findByText(/Connected acme/)).toBeTruthy();
  });

  test('a refused install shows the reason rather than a blank panel', async () => {
    server.use(
      http.post(`${BASE}/teams/${TEAM}/github/installations`, () =>
        HttpResponse.json({ detail: 'that installation does not belong to an account you can administer.' }, { status: 400 })),
      installLink({ configured: true, url: 'https://x' }),
      repos({ installations: [] }),
    );
    renderAt('/setup?installation_id=9&state=st&code=cd');

    expect(await screen.findByText(/does not belong to an account/)).toBeTruthy();
  });

  test('one broken installation does not hide the others', async () => {
    // A team that revoked one grant still needs to see and manage the rest.
    server.use(
      installLink({ configured: true, url: 'https://x' }),
      repos({
        installations: [
          { installation_id: 1, account_login: 'broken', repositories: [], error: 'GitHub refused a token' },
          ONE_INSTALL.installations[0],
        ],
      }),
    );
    renderAt('/setup');

    expect(await screen.findByText(/GitHub refused a token/)).toBeTruthy();
    expect(screen.getByText('acme/app')).toBeTruthy();
  });
});
