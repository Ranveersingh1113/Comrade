// Connecting a team's repositories.
//
// The install REDIRECT is not handled here — it lands on /github/setup, which
// is the App's one fixed Setup URL and therefore carries no team in its path.
// This component only ever shows what is already connected.
//
// The picker offers exactly what the installation already grants, and that is
// the point rather than a convenience: a member chooses from what they have
// granted us, instead of typing a name we would then have to decide whether to
// trust. A repository they have not granted cannot be reached by the token we
// mint for them anyway, so a free-text box would only produce failures that
// look like our bug.
//
// Connect and disconnect write straight to Supabase. RLS decides them —
// au_github_repos_insert requires team leadership AND an installation this
// same team owns — so there is one place that rule lives, and it is the place
// that also governs anyone posting to PostgREST directly.

import { useCallback, useEffect, useState } from 'react';
import {
  agentErrorText,
  githubInstallLink,
  githubRepositories,
  type InstallationRepos,
} from '../lib/agentApi';
import { supabase } from '../lib/supabase';

interface Props {
  teamId: string;
  isLeader: boolean;
}

export function GitHubConnect({ teamId, isLeader }: Props) {
  const [installUrl, setInstallUrl] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState<string | null>(null);
  const [installs, setInstalls] = useState<InstallationRepos[]>([]);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    if (!teamId) return;
    try {
      const link = await githubInstallLink(teamId);
      if (!link.configured) {
        setUnavailable(link.reason ?? 'GitHub is not set up for this deployment.');
        setInstallUrl(null);
      } else {
        setUnavailable(null);
        setInstallUrl(link.url ?? null);
      }
      const repos = await githubRepositories(teamId);
      setInstalls(repos.installations);
    } catch (e) {
      setNote(agentErrorText(e));
    } finally {
      setLoading(false);
    }
  }, [teamId]);

  useEffect(() => {
    void load();
  }, [load]);

  const setConnected = async (
    installationId: number,
    fullName: string,
    connect: boolean,
  ) => {
    setBusy(true);
    setNote(null);
    const { error } = connect
      ? await supabase.from('github_repos').insert({
          team_id: teamId,
          repo_full_name: fullName,
          installation_id: installationId,
        })
      : await supabase
          .from('github_repos')
          .delete()
          .eq('team_id', teamId)
          .eq('repo_full_name', fullName);
    if (error) setNote(error.message);
    setBusy(false);
    await load();
  };

  if (loading) {
    return <div style={{ fontSize: 12.5, color: 'var(--muted)', paddingLeft: 35 }}>loading…</div>;
  }

  if (unavailable) {
    return (
      <div style={{ fontSize: 12.5, color: 'var(--muted)', paddingLeft: 35, lineHeight: 1.55 }}>
        {unavailable} Comrade can still read a repository&apos;s history from webhook
        deliveries; connecting a working copy needs the App.
      </div>
    );
  }

  const anyRepos = installs.some((i) => i.repositories.length > 0);

  return (
    <div style={{ paddingLeft: 35 }}>
      {installs.map((inst) => (
        <div key={inst.installation_id} style={{ marginBottom: 14 }}>
          <div
            className="mono"
            style={{ fontSize: 11, color: 'var(--faint)', letterSpacing: '0.06em', marginBottom: 6 }}
          >
            {inst.account_login.toUpperCase()}
          </div>
          {inst.error && (
            <div style={{ fontSize: 12, color: 'var(--terracotta)' }}>{inst.error}</div>
          )}
          {inst.repositories.map((repo) => (
            <div
              key={repo.full_name}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 10,
                padding: '5px 0',
                fontSize: 13,
              }}
            >
              <span style={{ flex: 1 }}>{repo.full_name}</span>
              {repo.connected && (
                <span className="mono" style={{ fontSize: 10, color: 'var(--sage, #6b8f71)' }}>
                  CONNECTED
                </span>
              )}
              <button
                type="button"
                disabled={busy || !isLeader}
                onClick={() =>
                  void setConnected(inst.installation_id, repo.full_name, !repo.connected)
                }
                style={{
                  fontSize: 11,
                  padding: '3px 10px',
                  cursor: busy || !isLeader ? 'default' : 'pointer',
                  opacity: busy || !isLeader ? 0.5 : 1,
                }}
              >
                {repo.connected ? 'disconnect' : 'connect'}
              </button>
            </div>
          ))}
        </div>
      ))}

      {installs.length > 0 && !anyRepos && (
        <div style={{ fontSize: 12.5, color: 'var(--muted)', marginBottom: 10 }}>
          The App is installed but has not been granted any repositories yet. Add some
          on GitHub, then reload.
        </div>
      )}

      {installUrl && (
        <a
          href={installUrl}
          style={{ fontSize: 12.5, display: 'inline-block', marginTop: 4 }}
        >
          {installs.length ? 'Install on another account' : 'Install the Comrade GitHub App'}
        </a>
      )}

      {!isLeader && installs.length > 0 && (
        <div style={{ fontSize: 12, color: 'var(--faint)', marginTop: 8 }}>
          Only a team lead can connect or disconnect a repository.
        </div>
      )}

      {note && (
        <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 10 }}>{note}</div>
      )}
    </div>
  );
}
