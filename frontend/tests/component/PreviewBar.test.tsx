import { beforeEach, describe, expect, test, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

const { rows, getSession } = vi.hoisted(() => ({
  rows: { data: [] as unknown[], error: null as unknown },
  getSession: vi.fn(async () => ({ data: { session: { access_token: 't' } } })),
}));

vi.mock('../../src/lib/supabase', () => {
  const builder = {
    select: () => builder,
    eq: () => builder,
    order: () => builder,
    limit: async () => rows,
  };
  return { supabase: { from: () => builder, auth: { getSession } } };
});
vi.mock('../../src/hooks/useRealtime', () => ({ useTeamRealtime: () => {} }));

import { PreviewBar } from '../../src/components/PreviewBar';

const proc = (over: Record<string, unknown> = {}) => ({
  id: 'p-1', command: 'npm run dev', port: 3000, state: 'running',
  detail: null, started_at: '2026-09-06T10:00:00Z', ...over,
});

beforeEach(() => {
  rows.data = [];
  rows.error = null;
  getSession.mockClear();
  vi.restoreAllMocks();
});

describe('PreviewBar', () => {
  test('a running process offers a way in', async () => {
    rows.data = [proc()];
    render(<PreviewBar teamId="team-1" threadId="thread-1" />);
    expect(await screen.findByText('npm run dev')).toBeVisible();
    expect(await screen.findByRole('button', { name: /OPEN :3000/ })).toBeVisible();
  });

  test('a stopped process shows its state and no way in', async () => {
    rows.data = [proc({ state: 'stopped', detail: 'idle for too long' })];
    render(<PreviewBar teamId="team-1" threadId="thread-1" />);
    expect(await screen.findByText('STOPPED')).toBeVisible();
    expect(screen.queryByRole('button', { name: /OPEN/ })).toBeNull();
  });

  test('a process with no port cannot be previewed', async () => {
    rows.data = [proc({ port: null })];
    render(<PreviewBar teamId="team-1" threadId="thread-1" />);
    expect(await screen.findByText('npm run dev')).toBeVisible();
    expect(screen.queryByRole('button', { name: /OPEN/ })).toBeNull();
  });

  test('the tab is opened during the click, not after the grant returns', async () => {
    // 🔴 A window.open() after an await is no longer attributable to a user
    // gesture, and every browser blocks it. The previous version silently did
    // nothing on first click and worked only with popup blocking disabled.
    rows.data = [proc()];
    const tab = { location: { href: '' }, close: vi.fn() };
    const open = vi.spyOn(window, 'open').mockReturnValue(tab as never);
    let resolveFetch: (v: unknown) => void = () => {};
    vi.stubGlobal('fetch', vi.fn(() => new Promise((r) => { resolveFetch = r; })));

    render(<PreviewBar teamId="team-1" threadId="thread-1" />);
    await userEvent.click(await screen.findByRole('button', { name: /OPEN :3000/ }));

    expect(open).toHaveBeenCalled();          // already, while the fetch is pending
    resolveFetch({
      ok: true, status: 200,
      json: async () => ({ url: 'https://p-abc.previews.example.com/' }),
    });
    await waitFor(() => expect(tab.location.href).toBe('https://p-abc.previews.example.com/'));
  });

  test('an unconfigured deployment says so instead of failing silently', async () => {
    rows.data = [proc()];
    const tab = { location: { href: '' }, close: vi.fn() };
    vi.spyOn(window, 'open').mockReturnValue(tab as never);
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 503 })));

    render(<PreviewBar teamId="team-1" threadId="thread-1" />);
    await userEvent.click(await screen.findByRole('button', { name: /OPEN :3000/ }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/not configured/i);
    expect(tab.close).toHaveBeenCalled();     // no blank tab left behind
  });

  test('nothing renders when the thread has never started a process', async () => {
    const { container } = render(<PreviewBar teamId="team-1" threadId="thread-1" />);
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});
