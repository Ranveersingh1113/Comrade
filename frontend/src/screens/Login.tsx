import { useState } from 'react';
import { Navigate } from 'react-router-dom';
import { supabase } from '../lib/supabase';
import { useAuth } from '../state/AuthContext';

export function Login() {
  const { session } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [usePassword, setUsePassword] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (session) return <Navigate to="/" replace />;

  const sendLink = async () => {
    const target = email.trim();
    if (!target) return;
    setBusy(true);
    setError(null);
    const { error: err } = await supabase.auth.signInWithOtp({
      email: target,
      options: { emailRedirectTo: window.location.origin },
    });
    setBusy(false);
    if (err) setError(err.message);
    else setSent(true);
  };

  const signInPassword = async () => {
    const target = email.trim();
    if (!target || !password) return;
    setBusy(true);
    setError(null);
    const { error: err } = await supabase.auth.signInWithPassword({
      email: target,
      password,
    });
    setBusy(false);
    if (err) setError(err.message);
    // success: AuthContext picks up the session and Navigate fires
  };

  return (
    <div
      style={{
        height: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: 'var(--canvas)',
      }}
    >
      <div style={{ width: 380, padding: '0 24px' }}>
        <span className="orb breathing" style={{ width: 52, height: 52, fontSize: 20, marginBottom: 22 }}>
          ◈
        </span>
        <div className="display" style={{ fontSize: 40, lineHeight: 1.05, marginTop: 20 }}>
          Comrade
        </div>
        <div style={{ fontSize: 13, color: 'var(--muted)', marginTop: 10, lineHeight: 1.6 }}>
          The AI teammate for teams without a manager. Sign in with a magic link — no passwords.
        </div>
        {sent ? (
          <div
            className="card"
            style={{ marginTop: 26, padding: '17px 19px', fontSize: 13, lineHeight: 1.6 }}
          >
            Check <b>{email.trim()}</b> — a sign-in link is on its way.
          </div>
        ) : (
          <>
            <div className="composer" style={{ marginTop: 26 }}>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void (usePassword ? signInPassword() : sendLink());
                }}
                placeholder="you@university.edu"
                autoFocus
              />
              {!usePassword && (
                <button className="btn-ink" disabled={busy} onClick={() => void sendLink()}>
                  {busy ? '…' : 'SEND LINK'}
                </button>
              )}
            </div>
            {usePassword && (
              <div className="composer" style={{ marginTop: 10 }}>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') void signInPassword();
                  }}
                  placeholder="password"
                  aria-label="password"
                />
                <button className="btn-ink" disabled={busy} onClick={() => void signInPassword()}>
                  {busy ? '…' : 'SIGN IN'}
                </button>
              </div>
            )}
            <button
              onClick={() => setUsePassword((x) => !x)}
              className="mono"
              style={{
                marginTop: 14,
                border: 'none',
                background: 'transparent',
                color: 'var(--muted)',
                fontSize: 10.5,
                letterSpacing: '0.1em',
                cursor: 'pointer',
                padding: 0,
              }}
            >
              {usePassword ? '← MAGIC LINK INSTEAD' : 'USE A PASSWORD INSTEAD'}
            </button>
            {error && (
              <div style={{ marginTop: 12, fontSize: 12, color: 'var(--terracotta)' }}>{error}</div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
