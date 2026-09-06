import { beforeEach, describe, expect, test, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

const { membershipResult, membershipQuery } = vi.hoisted(() => {
  const membershipResult = vi.fn();
  const membershipQuery = {
    select: vi.fn(),
    eq: vi.fn(),
    in: vi.fn(),
  };
  membershipQuery.select.mockReturnValue(membershipQuery);
  membershipQuery.eq.mockReturnValue(membershipQuery);
  membershipQuery.in.mockImplementation(() => Promise.resolve(membershipResult()));
  return { membershipResult, membershipQuery };
});

vi.mock('../../src/lib/supabase', () => ({
  supabase: {
    from: vi.fn(() => membershipQuery),
  },
}));

vi.mock('../../src/state/AuthContext', () => ({
  useAuth: () => ({
    session: { user: { id: 'user-1' } },
    signOut: vi.fn(),
  }),
}));

import { TeamGate } from '../../src/screens/TeamGate';

describe('TeamGate loading failures', () => {
  beforeEach(() => {
    membershipResult.mockReset();
    membershipResult
      .mockReturnValueOnce({ data: null, error: { message: 'network down' } })
      .mockReturnValue({ data: [], error: null });
  });

  test('does not present a team outage as an empty account', async () => {
    render(
      <MemoryRouter>
        <TeamGate />
      </MemoryRouter>,
    );

    expect(await screen.findByRole('alert')).toHaveTextContent(/could not load your teams/i);
    expect(screen.queryByText(/no teams yet/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'CREATE' })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /try again/i }));

    expect(await screen.findByText(/no room yet/i)).toBeVisible();
    expect(screen.getByRole('button', { name: 'CREATE' })).toBeVisible();
  });
});