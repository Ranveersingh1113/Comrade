import { beforeEach, describe, expect, test, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

const { signInWithOAuth } = vi.hoisted(() => ({
  signInWithOAuth: vi.fn(),
}));

vi.mock('../../src/lib/supabase', () => ({
  supabase: {
    auth: { signInWithOAuth },
  },
}));

vi.mock('../../src/state/AuthContext', () => ({
  useAuth: () => ({ session: null }),
}));

import { Login } from '../../src/screens/Login';

describe('Login Google OAuth', () => {
  beforeEach(() => {
    signInWithOAuth.mockReset();
    signInWithOAuth.mockResolvedValue({ error: null });
  });

  test('starts Google OAuth with the current origin as the callback', async () => {
    render(<Login />);

    await userEvent.click(screen.getByRole('button', { name: /continue with google/i }));

    expect(signInWithOAuth).toHaveBeenCalledWith({
      provider: 'google',
      options: { redirectTo: window.location.origin },
    });
  });

  test('shows an OAuth startup error and lets the user retry', async () => {
    signInWithOAuth.mockResolvedValueOnce({ error: { message: 'Google sign-in is unavailable' } });
    render(<Login />);

    const button = screen.getByRole('button', { name: /continue with google/i });
    await userEvent.click(button);

    expect(await screen.findByText('Google sign-in is unavailable')).toBeVisible();
    expect(button).toBeEnabled();
  });
});