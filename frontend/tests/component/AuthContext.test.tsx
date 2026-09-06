import { beforeEach, describe, expect, test, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

const { getSession } = vi.hoisted(() => ({
  getSession: vi.fn(),
}));

vi.mock('../../src/lib/supabase', () => ({
  supabase: {
    auth: {
      getSession,
      onAuthStateChange: () => ({
        data: { subscription: { unsubscribe: vi.fn() } },
      }),
      signOut: vi.fn(),
    },
  },
}));

import { AuthProvider, displayNameFrom, useAuth } from '../../src/state/AuthContext';

function AuthProbe() {
  const { loading, error, retry } = useAuth();
  return (
    <div>
      <span>{loading ? 'loading' : 'ready'}</span>
      {error && <span role="alert">{error}</span>}
      <button onClick={retry}>Retry session</button>
    </div>
  );
}

describe('AuthProvider session bootstrap', () => {
  beforeEach(() => {
    getSession.mockReset();
  });

  test('leaves the loading state and can retry after a session failure', async () => {
    getSession
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValueOnce({ data: { session: null } });

    render(
      <AuthProvider>
        <AuthProbe />
      </AuthProvider>,
    );

    expect(await screen.findByRole('alert')).toHaveTextContent(/could not verify your session/i);
    expect(screen.getByText('ready')).toBeVisible();

    await userEvent.click(screen.getByRole('button', { name: /retry session/i }));

    expect(await screen.findByText('ready')).toBeVisible();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(getSession).toHaveBeenCalledTimes(2);
  });
});
describe('what a new member is called', () => {
  const user = (metadata: Record<string, unknown>, email = 'priya.sharma@acme.dev') =>
    ({ id: 'u1', email, user_metadata: metadata }) as never;

  test('a Google account uses the real name, not the email prefix', () => {
    // Google never sends `display_name`. Before this, someone who signed in
    // with the Google button appeared to their team as "priya.sharma" while
    // the same person on a password appeared as "Priya Sharma" — and this
    // string sits beside every message they send.
    expect(displayNameFrom(user({ full_name: 'Priya Sharma' }))).toBe('Priya Sharma');
    expect(displayNameFrom(user({ name: 'Priya Sharma' }))).toBe('Priya Sharma');
  });

  test('an explicit display_name still wins', () => {
    expect(
      displayNameFrom(user({ display_name: 'Priya', full_name: 'Priya Sharma' })),
    ).toBe('Priya');
  });

  test('blank metadata does not become a blank name', () => {
    // A provider that sends an empty string is not providing a name, and a
    // nameless row renders as a gap next to a message.
    expect(displayNameFrom(user({ full_name: '   ' }))).toBe('priya.sharma');
  });

  test('no email and no metadata still yields something printable', () => {
    expect(displayNameFrom(user({}, ''))).toBe('Member');
  });
});
