import { useCallback, useEffect, useState } from 'react';
import { supabase } from '../lib/supabase';
import { useTeamRealtime } from '../hooks/useRealtime';

/** A process the agent started in this thread. Mirrors sandbox_processes;
 *  `authenticated` may read these rows and nothing else, which is why there is
 *  no start or stop control here. */
export interface SandboxProcess {
  id: string;
  command: string;
  port: number | null;
  state: 'starting' | 'running' | 'exited' | 'failed' | 'stopped' | 'expired';
  detail: string | null;
  started_at: string;
}

const LIVE = new Set(['starting', 'running']);

const LABEL: Record<SandboxProcess['state'], string> = {
  starting: 'STARTING',
  running: 'RUNNING',
  exited: 'EXITED',
  failed: 'FAILED',
  stopped: 'STOPPED',
  expired: 'EXPIRED',
};

/**
 * What is running in this thread, and a way in.
 *
 * The link is minted on CLICK rather than rendered into the page. A preview
 * token is a bearer credential with a short life; putting one in the DOM of
 * every open thread would mean a screenshot of this room is a working link to
 * the team's server, and would expire while the tab sat open besides.
 */
export function PreviewBar({ teamId, threadId }: { teamId: string; threadId: string }) {
  const [procs, setProcs] = useState<SandboxProcess[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [opening, setOpening] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!teamId) return;
    const { data, error: err } = await supabase
      .from('sandbox_processes')
      .select('id, command, port, state, detail, started_at')
      .eq('thread_id', threadId)
      .order('started_at', { ascending: false })
      .limit(5);
    if (err) setError(err.message);
    else {
      setProcs((data as SandboxProcess[] | null) ?? []);
      setError(null);
    }
  }, [teamId, threadId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  useTeamRealtime('sandbox_processes', teamId, refresh, `thread_id=eq.${threadId}`);

  const open = async (proc: SandboxProcess) => {
    setOpening(proc.id);
    setError(null);
    // 🔴 OPENED SYNCHRONOUSLY, inside the click. A window.open() that happens
    // after an await is no longer attributable to a user gesture, and every
    // browser blocks it — so the previous version silently did nothing on the
    // first click and worked only if the member disabled their popup blocker.
    // The tab is opened now and navigated once the grant exists.
    const tab = window.open('', '_blank', 'noopener,noreferrer');
    try {
      const { data } = await supabase.auth.getSession();
      const base = (import.meta.env.VITE_AGENT_API_URL as string | undefined) ?? '';
      const resp = await fetch(
        `${base}/previews/${proc.id}?team_id=${encodeURIComponent(teamId)}`,
        {
          method: 'POST',
          headers: { Authorization: `Bearer ${data.session?.access_token ?? ''}` },
        },
      );
      if (!resp.ok) {
        tab?.close();
        setError(
          resp.status === 503
            ? 'Previews are not configured on this deployment.'
            : 'That preview is not available.',
        );
        return;
      }
      // ABSOLUTE, and on a different origin: the preview lives on its own
      // hostname so it cannot read this page's storage. Never prefix it with
      // the API base — that would put it back on our origin.
      const grant = (await resp.json()) as { url: string };
      if (tab) tab.location.href = grant.url;
      else window.open(grant.url, '_blank', 'noopener,noreferrer');
    } catch {
      tab?.close();
      setError('Could not reach the preview.');
    } finally {
      setOpening(null);
    }
  };

  const live = procs.filter((p) => LIVE.has(p.state));
  if (!procs.length) return null;

  return (
    <div style={{ padding: '8px 28px', display: 'grid', gap: 6 }}>
      {procs.slice(0, live.length ? undefined : 1).map((proc) => (
        <div
          key={proc.id}
          data-process-state={proc.state}
          style={{
            display: 'flex', alignItems: 'center', gap: 10,
            fontSize: 12, color: 'var(--muted)',
          }}
        >
          <span
            className="mono"
            style={{ fontSize: 9.5, letterSpacing: '0.1em', opacity: 0.8 }}
          >
            {LABEL[proc.state]}
          </span>
          <span style={{ fontFamily: 'var(--mono, monospace)' }}>{proc.command}</span>
          {LIVE.has(proc.state) && proc.port ? (
            <button
              className="btn-ghost"
              disabled={opening === proc.id}
              onClick={() => void open(proc)}
              style={{ fontSize: 11, padding: '2px 10px' }}
            >
              {opening === proc.id ? 'OPENING…' : `OPEN :${proc.port}`}
            </button>
          ) : null}
          {proc.detail && !LIVE.has(proc.state) ? (
            <span style={{ opacity: 0.8 }}>{proc.detail}</span>
          ) : null}
        </div>
      ))}
      {error && (
        <div role="alert" style={{ fontSize: 12, color: 'var(--terracotta)' }}>
          {error}
        </div>
      )}
    </div>
  );
}
