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

import { AuthProvider, useAuth } from '../../src/state/AuthContext';

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