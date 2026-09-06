// Where GitHub sends someone after they install the App.
//
// A GitHub App has ONE fixed Callback URL. It cannot carry a team in its path, so
// this route knows nothing about which team it is completing an install for —
// the signed `state` token does, and the server reads the team out of it and
// tells us where to go next.
//
// That is why this is its own screen rather than something the setup page
// handles: a component under /t/:teamId could never have been reached by this
// redirect, because there is no team in the URL GitHub sends people to.
//
// It is the CALLBACK URL rather than the Setup URL, and that is GitHub's rule
// rather than a preference: ticking "Request user authorization (OAuth) during
// installation" — which is what sends the `code` this flow depends on —
// disables the Setup URL field outright.

import { useEffect, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { agentErrorText, githubRecordInstallation } from '../lib/agentApi';

export function GitHubCallback() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  // React runs effects twice in StrictMode, and this one is not idempotent
  // from the user's side: the second POST reuses a `code` GitHub has already
  // spent, which fails with a message about identity that has nothing to do
  // with what went wrong.
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;

    const installationId = params.get('installation_id');
    const state = params.get('state');
    const code = params.get('code');

    if (!installationId || !state || !code) {
      setError(
        'GitHub did not send everything needed to finish this install. Start '
        + 'again from your team’s setup screen.',
      );
      return;
    }

    void (async () => {
      try {
        const out = await githubRecordInstallation(Number(installationId), state, code);
        // replace, not push: the URL still holds a spent code and a used state
        // token, and Back should not return to a page that would retry them.
        navigate(`/t/${out.team_id}/setup`, { replace: true });
      } catch (e) {
        setError(agentErrorText(e));
      }
    })();
  }, [params, navigate]);

  return (
    <main className="callback-screen card" style={{ padding: '34px 32px', maxWidth: 520, margin: '64px auto' }}>
      <div className="micro-label">External connection</div>
      <h1 className="display" style={{ fontSize: 26, marginBottom: 10 }}>
        {error ? 'That install did not finish' : 'Connecting GitHub…'}
      </h1>
      {error ? (
        <>
          <p style={{ fontSize: 13.5, color: 'var(--muted)', lineHeight: 1.6 }}>{error}</p>
          <button type="button" onClick={() => navigate('/teams')} style={{ marginTop: 14 }}>
            Back to your teams
          </button>
        </>
      ) : (
        <p style={{ fontSize: 13.5, color: 'var(--muted)' }}>
          Checking with GitHub that this installation belongs to your team.
        </p>
      )}
    </main>
  );
}
