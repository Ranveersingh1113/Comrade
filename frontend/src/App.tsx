import { useEffect, useState } from 'react';
import {
  BrowserRouter,
  Navigate,
  Outlet,
  Route,
  Routes,
  useLocation,
  useParams,
} from 'react-router-dom';
import { ErrorBoundary } from './components/ErrorBoundary';
import { AuthProvider, useAuth } from './state/AuthContext';
import { TeamProvider, useTeam } from './state/TeamContext';
import { Sidebar } from './components/Sidebar';
import { useIsNarrow } from './hooks/useIsNarrow';
import { Login } from './screens/Login';
import { TeamGate } from './screens/TeamGate';
import { Tasks } from './screens/Tasks';
import { Wiki } from './screens/Wiki';
import { Documents } from './screens/Documents';
import { ConsentInbox } from './screens/ConsentInbox';
import { Setup } from './screens/Setup';
import { Team } from './screens/Team';
import { GitHubCallback } from './screens/GitHubCallback';
import { LegacyThreadRedirect, Threads } from './screens/Threads';

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { session, loading } = useAuth();
  if (loading) {
    return (
      <div
        style={{
          height: '100vh',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        <span className="orb breathing" style={{ width: 44, height: 44, fontSize: 17 }}>
          ◈
        </span>
      </div>
    );
  }
  if (!session) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

function TeamShell() {
  const { teamId } = useParams<{ teamId: string }>();
  if (!teamId) return <Navigate to="/teams" replace />;
  return (
    <TeamProvider teamId={teamId}>
      <TeamShellInner />
    </TeamProvider>
  );
}

function TeamShellInner() {
  const location = useLocation();
  const narrow = useIsNarrow();
  const { access } = useTeam();
  const [navOpen, setNavOpen] = useState(false);

  // Any navigation closes the drawer. Without this a member taps "Tasks" and
  // lands on a screen still covered by the menu they just used.
  useEffect(() => setNavOpen(false), [location.pathname]);

  // A team you are not in renders as a complete shell full of nothing: no
  // name, no roster, no messages, no error — indistinguishable from a quiet
  // team. Two supported paths reach it now: leaving a team and then pressing
  // Back, and a stale `comrade.teamId` pointing at a team that is gone.
  // 'unknown' deliberately does NOT redirect; a failed request is not a
  // refusal, and the screens below have their own error states for it.
  if (access === 'denied') {
    localStorage.removeItem('comrade.teamId');
    return <Navigate to="/teams" replace />;
  }

  return (
    <>
      <div style={{ display: 'flex', height: '100dvh', overflow: 'hidden' }}>
        {/* Wide: the sidebar is simply there. Narrow: it is off-canvas, so it
            must not occupy layout space at all — `display: none` rather than a
            transform, because a translated 250px column still forces the
            document wider than the viewport and brings back the horizontal
            scroll this whole change exists to remove. */}
        {(!narrow || navOpen) && (
          <div
            style={
              narrow
                ? {
                    position: 'fixed',
                    inset: 0,
                    zIndex: 40,
                    display: 'flex',
                    background: 'rgba(20,18,28,0.45)',
                  }
                : { display: 'flex' }
            }
            onClick={narrow ? () => setNavOpen(false) : undefined}
          >
            <Sidebar onNavigate={narrow ? () => setNavOpen(false) : undefined} />
          </div>
        )}

        <div
          style={{
            flex: 1,
            minWidth: 0,
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden',
          }}
        >
          {narrow && (
            <button
              type="button"
              aria-label="Open navigation"
              aria-expanded={navOpen}
              onClick={() => setNavOpen(true)}
              style={{
                flex: 'none',
                display: 'flex',
                alignItems: 'center',
                gap: 10,
                padding: '12px 16px',
                border: 'none',
                borderBottom: '1px solid var(--border-soft)',
                background: 'var(--ink)',
                color: 'var(--paper)',
                font: 'inherit',
                fontSize: 12,
                letterSpacing: '0.18em',
                textTransform: 'uppercase',
                cursor: 'pointer',
              }}
            >
              <span aria-hidden>☰</span> Menu
            </button>
          )}
          <ErrorBoundary key={location.pathname}>
            <Outlet />
          </ErrorBoundary>
        </div>
      </div>
    </>
  );
}

function Home() {
  const stored = localStorage.getItem('comrade.teamId');
  return <Navigate to={stored ? `/t/${stored}/threads` : '/teams'} replace />;
}

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<Login />} />
          {/* A GitHub App has ONE fixed Setup URL, so this route carries no
              team — the signed state token does, and the server reads it. */}
          <Route
            path="/github/setup"
            element={
              <RequireAuth>
                <GitHubCallback />
              </RequireAuth>
            }
          />
          <Route
            path="/teams"
            element={
              <RequireAuth>
                <TeamGate />
              </RequireAuth>
            }
          />
          <Route
            path="/t/:teamId"
            element={
              <RequireAuth>
                <TeamShell />
              </RequireAuth>
            }
          >
            <Route index element={<Navigate to="threads" replace />} />
            <Route path="threads" element={<Threads />} />
            <Route path="threads/:threadId" element={<Threads />} />
            <Route path="room" element={<LegacyThreadRedirect />} />
            <Route path="thread" element={<LegacyThreadRedirect privateThread />} />
            <Route path="tasks" element={<Tasks />} />
            <Route path="wiki" element={<Wiki />} />
            <Route path="docs" element={<Documents />} />
            <Route path="inbox" element={<ConsentInbox />} />
            <Route path="setup" element={<Setup />} />
            <Route path="team" element={<Team />} />
          </Route>
          <Route
            path="/"
            element={
              <RequireAuth>
                <Home />
              </RequireAuth>
            }
          />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  );
}
