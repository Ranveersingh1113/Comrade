/**
 * Shared seams for component tests.
 *
 * Usage in a test file (vi.mock is hoisted, so factories live here):
 *
 *   vi.mock('../../src/lib/supabase', () => makeSupabaseMock());
 *   vi.mock('../../src/state/TeamContext', () => makeTeamMock());
 *
 * Then seed `supaState.tables`, read `supaState.inserts`, and adjust
 * `teamState` per test. `resetMocks()` in beforeEach.
 */
import type { ReactNode } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { render } from '@testing-library/react';
import { setupServer } from 'msw/node';
import type { Profile, Team } from '../../src/lib/types';

// ---------- supabase module mock ----------

interface WriteRecord {
  table: string;
  values: unknown;
}

export const supaState = {
  /** rows returned for any select on a table — filters are ignored on
   * purpose; correctness of filtering belongs to the integration layer. */
  tables: {} as Record<string, unknown[]>,
  inserts: [] as WriteRecord[],
  updates: [] as WriteRecord[],
  /** set to make the next insert into a table fail */
  insertErrors: {} as Record<string, string>,
};

export function resetSupa(): void {
  supaState.tables = {};
  supaState.inserts = [];
  supaState.updates = [];
  supaState.insertErrors = {};
}

function makeQuery(table: string) {
  const rows = () => supaState.tables[table] ?? [];
  const q: Record<string, unknown> = {};
  const chain = () => q;
  Object.assign(q, {
    select: chain, eq: chain, neq: chain, in: chain, is: chain, not: chain,
    gte: chain, lte: chain, order: chain, limit: chain,
    single: () => Promise.resolve({ data: rows()[0] ?? null, error: null }),
    maybeSingle: () => Promise.resolve({ data: rows()[0] ?? null, error: null }),
    insert: (values: unknown) => {
      const message = supaState.insertErrors[table];
      if (message) return Promise.resolve({ data: null, error: { message } });
      supaState.inserts.push({ table, values });
      return Promise.resolve({ data: null, error: null });
    },
    update: (values: unknown) => ({
      eq: () => {
        supaState.updates.push({ table, values });
        return Promise.resolve({ data: null, error: null });
      },
    }),
    then: (resolve: (v: { data: unknown[]; error: null; count: number }) => unknown) =>
      Promise.resolve({ data: rows(), error: null, count: rows().length }).then(resolve),
  });
  return q;
}

export function makeSupabaseMock() {
  return {
    supabase: {
      from: (table: string) => makeQuery(table),
      auth: {
        getSession: () =>
          Promise.resolve({
            data: { session: { access_token: 'test-token', user: { id: teamState.myUserId } } },
          }),
      },
      channel: () => {
        const ch = { on: () => ch, subscribe: () => ch };
        return ch;
      },
      removeChannel: () => Promise.resolve('ok'),
    },
  };
}

// ---------- TeamContext module mock ----------

const P1: Profile = {
  id: 'u1', display_name: 'Priya Sharma', email: null, github_username: null,
  created_at: '2026-07-01T00:00:00Z',
};
const P2: Profile = {
  id: 'u2', display_name: 'Marcus Lee', email: null, github_username: null,
  created_at: '2026-07-01T00:00:00Z',
};

export const teamState = {
  team: { id: 'team-1', name: 'MealShare', created_by: 'u1', created_at: '2026-07-01' } as Team,
  roster: [
    { membership: { user_id: 'u1', role: 'leader', status: 'active' }, profile: P1 },
    { membership: { user_id: 'u2', role: 'member', status: 'active' }, profile: P2 },
  ],
  myUserId: 'u1',
  isLeader: true,
  loading: false,
};

export function resetTeam(): void {
  teamState.myUserId = 'u1';
  teamState.isLeader = true;
}

export function makeTeamMock() {
  return {
    useTeam: () => ({
      ...teamState,
      profileOf: (id: string | null) =>
        teamState.roster.find((r) => r.profile.id === id)?.profile ?? null,
      refreshRoster: () => Promise.resolve(),
    }),
    TeamProvider: ({ children }: { children: ReactNode }) => children,
  };
}

export function makeAuthMock() {
  return {
    useAuth: () => ({
      session: {
        access_token: 'test-token',
        user: { id: teamState.myUserId, email: 'test@test.dev' },
      },
      loading: false,
      signOut: () => Promise.resolve(),
    }),
    AuthProvider: ({ children }: { children: ReactNode }) => children,
  };
}

// ---------- MSW server for the FastAPI surface ----------

export const server = setupServer();

// ---------- render helper ----------

export function renderInApp(ui: ReactNode) {
  return render(<MemoryRouter>{ui}</MemoryRouter>);
}
