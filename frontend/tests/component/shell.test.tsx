import { beforeEach, describe, expect, test, vi } from 'vitest';
import { screen } from '@testing-library/react';
import {
  makeAuthMock, makeSupabaseMock, makeTeamMock, renderInApp, resetSupa, resetTeam,
  supaState,
} from './mocks';

vi.mock('../../src/lib/supabase', () => makeSupabaseMock());
vi.mock('../../src/state/TeamContext', () => makeTeamMock());
vi.mock('../../src/state/AuthContext', () => makeAuthMock());

import { ErrorBoundary } from '../../src/components/ErrorBoundary';
import { Sidebar } from '../../src/components/Sidebar';

beforeEach(() => {
  resetSupa();
  resetTeam();
});

describe('ErrorBoundary contains a crashing screen', () => {
  function Bomb(): never {
    throw new Error('kaboom from a screen');
  }

  test('fallback renders; siblings stay mounted (the double-subscribe regression)', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    renderInApp(
      <>
        <div>sidebar stays alive</div>
        <ErrorBoundary>
          <Bomb />
        </ErrorBoundary>
      </>,
    );
    spy.mockRestore();
    expect(screen.getByText('This screen hit an error.')).toBeInTheDocument();
    expect(screen.getByText(/kaboom from a screen/)).toBeInTheDocument();
    expect(screen.getByText('sidebar stays alive')).toBeInTheDocument();
  });
});

describe('Sidebar signals', () => {
  test('pending consent count badges the inbox item', async () => {
    supaState.tables.consent_queue = [
      { id: 'c1', status: 'pending' }, { id: 'c2', status: 'pending' },
    ];
    renderInApp(<Sidebar />);
    expect(await screen.findByText(/2 consents awaiting key/)).toBeInTheDocument();
  });

  test('all clear when nothing is pending', async () => {
    supaState.tables.consent_queue = [];
    renderInApp(<Sidebar />);
    expect(await screen.findByText(/watching · all clear/)).toBeInTheDocument();
  });
});
