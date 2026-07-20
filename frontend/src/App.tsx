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
import { TeamProvider } from './state/TeamContext';
import { Sidebar } from './components/Sidebar';
import { Login } from './screens/Login';
import { TeamGate } from './screens/TeamGate';
import { GroupRoom } from './screens/GroupRoom';
import { PrivateThread } from './screens/PrivateThread';
import { Tasks } from './screens/Tasks';
import { Wiki } from './screens/Wiki';
import { Documents } from './screens/Documents';
import { ConsentInbox } from './screens/ConsentInbox';
import { Setup } from './screens/Setup';

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
  const location = useLocation();
  if (!teamId) return <Navigate to="/teams" replace />;
  return (
    <TeamProvider teamId={teamId}>
      <div style={{ display: 'flex', height: '100vh', overflow: 'hidden' }}>
        <Sidebar />
        <ErrorBoundary key={location.pathname}>
          <Outlet />
        </ErrorBoundary>
      </div>
    </TeamProvider>
  );
}

function Home() {
  const stored = localStorage.getItem('comrade.teamId');
  return <Navigate to={stored ? `/t/${stored}/room` : '/teams'} replace />;
}

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<Login />} />
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
            <Route index element={<Navigate to="room" replace />} />
            <Route path="room" element={<GroupRoom />} />
            <Route path="thread" element={<PrivateThread />} />
            <Route path="tasks" element={<Tasks />} />
            <Route path="wiki" element={<Wiki />} />
            <Route path="docs" element={<Documents />} />
            <Route path="inbox" element={<ConsentInbox />} />
            <Route path="setup" element={<Setup />} />
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
