// Connecting a team's repositories.
//
// The install REDIRECT is not handled here — it lands on /github/setup, which
// is the App's one fixed Callback URL and therefore carries no team in its path.
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

/** Colour per environment state. `stale` and `failed` are warned about rather
 *  than merely reported: a PASS from a stale environment is the more dangerous
 *  result, because it is the one somebody acts on. */
const ENV_COLOUR: Record<string, string> = {
  ready: 'var(--sage, #6b8f71)',
  building: 'var(--faint)',
  none: 'var(--faint)',
  disabled: 'var(--faint)',
  stale: 'var(--peach)',
  failed: 'var(--terracotta)',
  unknown: 'var(--faint)',
};

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

  // POLL WHILE A CLONE IS IN FLIGHT, and only then.
  //
  // A repository row exists the moment it is connected; the clone happens on
  // the worker afterwards. Loading once on mount meant the screen said
  // CLONING… until someone thought to reload — so a clone that finished in
  // four seconds looked indistinguishable from one that had hung, and the
  // honest state read as a broken one.
  //
  // Stops as soon as nothing is pending, so a settled screen makes no
  // requests. A failed clone is settled too: it has an answer, and repeating
  // the question will not change it before the reconciler's own backoff.
  const building = installs
    .flatMap((i) => i.repositories)
    .some((r) => r.environment?.status === 'building'
              || r.environment?.status === 'none');

  const cloning = installs
    .flatMap((i) => i.repositories)
    .some((r) => r.connected && !r.cloned_at && !r.sync_error);

  useEffect(() => {
    if (!cloning && !building) return undefined;
    const id = setInterval(() => void load(), 4000);
    return () => clearInterval(id);
  }, [cloning, building, load]);

  /** Turn the dependency environment on or off for one repository.
   *
   * A direct Supabase write, like connect/disconnect: au_github_repos_update
   * already requires team leadership and a matching installation, so the rule
   * lives in the policy rather than in a route that anyone could post around.
   * The worker holds only a COLUMN grant on the status fields and cannot set
   * this — it reports what the environment is doing, never whether the team
   * asked for one. */
  const setEnvEnabled = async (fullName: string, enabled: boolean) => {
    setBusy(true);
    setNote(null);
    const { error } = await supabase
      .from('github_repos')
      .update({ env_enabled: enabled })
      .eq('team_id', teamId)
      .eq('repo_full_name', fullName);
    if (error) setNote(error.message);
    setBusy(false);
    await load();
  };

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
    return <div className="github-loading" aria-label="Loading GitHub connections"><span className="skeleton" /><span className="skeleton" /></div>;
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
    <div className="github-connect" style={{ paddingLeft: 35 }}>
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
            <div className="repo-row" key={repo.full_name}>
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 10,
                padding: '5px 0',
                fontSize: 13,
              }}
            >
              <span style={{ flex: 1 }}>{repo.full_name}</span>
              {/* THREE states, not two. "Connected" on its own was a lie
                  whenever the clone had failed: this screen said CONNECTED
                  while the agent said no repository was connected, and
                  nothing anywhere named the credential error behind it. */}
              {repo.connected && repo.sync_error && (
                <span
                  className="mono"
                  title={repo.sync_error}
                  style={{ fontSize: 10, color: 'var(--terracotta)' }}
                >
                  COULD NOT CLONE
                </span>
              )}
              {repo.connected && !repo.sync_error && !repo.cloned_at && (
                <span className="mono" style={{ fontSize: 10, color: 'var(--faint)' }}>
                  CLONING…
                </span>
              )}
              {repo.connected && !repo.sync_error && repo.cloned_at && (
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
            {/* The dependency environment, for connected repositories only.
                Installing runs the repo's own build hooks with network access,
                so it is never implied by connecting — a team turns it on, and
                then needs to be able to see what it is doing. An environment
                that silently is not there turns every red test suite into a
                mystery. */}
            {repo.connected && repo.environment && (
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  padding: '0 0 7px 14px',
                  fontSize: 11.5,
                  color: 'var(--muted)',
                }}
              >
                <span className="mono" style={{ fontSize: 10, color: 'var(--faint)' }}>
                  ENV
                </span>
                <span
                  className="mono"
                  title={repo.environment.detail}
                  style={{ fontSize: 10, color: ENV_COLOUR[repo.environment.status] }}
                >
                  {repo.environment.status.toUpperCase()}
                </span>
                <span style={{ flex: 1, fontSize: 11.5 }}>
                  {repo.environment.detail}
                </span>
                <button
                  type="button"
                  disabled={busy || !isLeader}
                  onClick={() =>
                    void setEnvEnabled(
                      repo.full_name, repo.environment?.status === 'disabled',
                    )
                  }
                  style={{
                    fontSize: 10.5,
                    padding: '2px 8px',
                    cursor: busy || !isLeader ? 'default' : 'pointer',
                    opacity: busy || !isLeader ? 0.5 : 1,
                  }}
                >
                  {repo.environment.status === 'disabled'
                    ? 'set up environment'
                    : 'turn off'}
                </button>
              </div>
            )}
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

      {installs.flatMap((i) => i.repositories).filter((r) => r.sync_error).map((r) => (
        <div
          key={`err-${r.full_name}`}
          style={{ fontSize: 12, color: 'var(--terracotta)', marginTop: 8, lineHeight: 1.5 }}
        >
          {r.full_name}: {r.sync_error}
        </div>
      ))}

      {note && (
        <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 10 }}>{note}</div>
      )}
    </div>
  );
}
