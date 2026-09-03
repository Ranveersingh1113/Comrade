import { afterAll, afterEach, beforeAll, describe, expect, test, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { http, HttpResponse } from 'msw';
import { makeSupabaseMock, makeTeamMock, server } from './mocks';

vi.mock('../../src/lib/supabase', () => makeSupabaseMock());
vi.mock('../../src/state/TeamContext', () => makeTeamMock());

import { GitHubCallback } from '../../src/screens/GitHubCallback';

const BASE = 'http://localhost:8000';
const TEAM = 'team-1';

function renderAt(query: string) {
  return render(
    <MemoryRouter initialEntries={[`/github/setup${query}`]}>
      <Routes>
        <Route path="/github/setup" element={<GitHubCallback />} />
        <Route path="/t/:teamId/setup" element={<div>SETUP FOR THE TEAM</div>} />
        <Route path="/teams" element={<div>TEAMS</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('GitHubCallback', () => {
  test('finishes the install and lands on the right team', async () => {
    // 🔴 The route carries NO team, and cannot: a GitHub App has one fixed
    // Setup URL. The team comes back from the server, which read it out of the
    // signed state token.
    let received: unknown = null;
    server.use(
      http.post(`${BASE}/github/installations`, async ({ request }) => {
        received = await request.json();
        return HttpResponse.json({
          team_id: TEAM, installation_id: 42, account_login: 'acme',
        });
      }),
    );
    renderAt('?installation_id=42&state=st&code=cd&setup_action=install');

    expect(await screen.findByText('SETUP FOR THE TEAM')).toBeTruthy();
    expect(received).toEqual({ installation_id: 42, state: 'st', code: 'cd' });
  });

  test('posts once even though StrictMode runs effects twice', async () => {
    // The second POST would reuse a `code` GitHub has already spent, and fail
    // with a message about identity that has nothing to do with what is wrong.
    let calls = 0;
    server.use(
      http.post(`${BASE}/github/installations`, () => {
        calls += 1;
        return HttpResponse.json({
          team_id: TEAM, installation_id: 42, account_login: 'acme',
        });
      }),
    );
    render(
      <MemoryRouter initialEntries={['/github/setup?installation_id=42&state=st&code=cd']}>
        <Routes>
          <Route path="/github/setup" element={<GitHubCallback />} />
          <Route path="/t/:teamId/setup" element={<div>SETUP FOR THE TEAM</div>} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText('SETUP FOR THE TEAM');
    expect(calls).toBe(1);
  });

  test('a refusal is shown, not swallowed into a spinner', async () => {
    server.use(
      http.post(`${BASE}/github/installations`, () =>
        HttpResponse.json(
          { detail: 'that installation does not belong to an account you can administer.' },
          { status: 400 },
        )),
    );
    renderAt('?installation_id=9&state=st&code=cd');

    expect(await screen.findByText(/does not belong to an account/)).toBeTruthy();
    expect(screen.getByText(/did not finish/)).toBeTruthy();
  });

  test('a redirect missing its parameters says so instead of posting', async () => {
    // No handler is registered, so onUnhandledRequest:'error' fails the test
    // if this posts anyway — which is the assertion that matters.
    renderAt('?setup_action=install');
    expect(await screen.findByText(/did not send everything needed/)).toBeTruthy();
  });
});
