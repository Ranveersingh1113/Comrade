import { useState } from 'react';
import { Navigate } from 'react-router-dom';
import { supabase } from '../lib/supabase';
import { useAuth } from '../state/AuthContext';
import { OrbLogo } from '../components/Avatar';

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
    if (!target || !target.includes('@')) {
      setError('Enter a valid email address.');
      return;
    }
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

  const signInGoogle = async () => {
    setBusy(true);
    setError(null);
    const { error: err } = await supabase.auth.signInWithOAuth({
      provider: 'google',
      options: { redirectTo: window.location.origin },
    });
    if (err) {
      setError(err.message);
      setBusy(false);
    }
  };

  const signInPassword = async () => {
    const target = email.trim();
    if (!target || !target.includes('@')) {
      setError('Enter a valid email address.');
      return;
    }
    if (!password) {
      setError('Enter your password.');
      return;
    }
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
        minHeight: '100dvh',
        display: 'flex',
        alignItems: 'stretch',
        background: 'var(--canvas)',
        position: 'relative',
        overflow: 'hidden',
      }}
    >
      <div className="login-grid" style={{ width: 'min(100%, 1160px)', margin: 'auto', display: 'grid', gridTemplateColumns: 'minmax(0, 1.15fr) minmax(320px, 420px)', gap: 64, alignItems: 'center', padding: '56px 32px' }}>
        <div style={{ maxWidth: 570 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, color: 'var(--ink)' }}>
            <OrbLogo size={48} breathing />
            <span className="mono" style={{ fontSize: 12, letterSpacing: '.2em', textTransform: 'uppercase' }}>COMRADE / TEAM ROOM</span>
          </div>
          <div className="display" style={{ fontSize: 'clamp(54px, 8vw, 96px)', lineHeight: .91, marginTop: 34, color: 'var(--ink)' }}>
            Work that<br /><em>remembers.</em>
          </div>
          <div style={{ fontSize: 15, color: 'var(--text-soft)', marginTop: 24, lineHeight: 1.7, maxWidth: 430 }}>
            A shared room for decisions, context, and the work between them. Comrade listens carefully, keeps the thread intact, and asks before it acts.
          </div>
          <div className="mono" style={{ display: 'flex', gap: 18, flexWrap: 'wrap', marginTop: 34, fontSize: 10, letterSpacing: '.12em', color: 'var(--muted)' }}>
            <span>TEAM MEMORY</span><span>VISIBLE AGENCY</span><span>CONSENT FIRST</span>
          </div>
        </div>
        <div className="card" style={{ padding: '28px 26px', boxShadow: '7px 7px 0 rgba(32,45,53,.11)' }}>
          <div className="micro-label">Enter your team room</div>
          <div style={{ fontSize: 13, color: 'var(--text-soft)', marginTop: 8, lineHeight: 1.5 }}>
            Sign in with the account your teammates know.
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
            <button
              type="button"
              disabled={busy}
              onClick={() => void signInGoogle()}
              aria-label="Continue with Google"
              style={{
                width: '100%',
                marginTop: 26,
                minHeight: 44,
                border: '1px solid var(--line)',
                borderRadius: 10,
                background: 'var(--surface)',
                color: 'var(--ink)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 10,
                cursor: busy ? 'wait' : 'pointer',
                fontSize: 13,
                fontWeight: 600,
              }}
            >
              <svg aria-hidden="true" width="18" height="18" viewBox="0 0 18 18">
                <path fill="#4285F4" d="M17.64 9.205c0-.639-.057-1.252-.164-1.841H9v3.482h4.844a4.14 4.14 0 0 1-1.797 2.715v2.258h2.909c1.703-1.568 2.684-3.879 2.684-6.614Z" />
                <path fill="#34A853" d="M9 18c2.43 0 4.467-.806 5.956-2.181l-2.909-2.258c-.806.54-1.835.859-3.047.859-2.344 0-4.328-1.584-5.037-3.711H.956v2.332A9 9 0 0 0 9 18Z" />
                <path fill="#FBBC05" d="M3.963 10.709A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.169.281-1.709V4.959H.956A9 9 0 0 0 0 9c0 1.452.347 2.827.956 4.041l3.007-2.332Z" />
                <path fill="#EA4335" d="M9 3.58c1.321 0 2.507.454 3.441 1.346l2.581-2.581C13.463.892 11.426 0 9 0A9 9 0 0 0 .956 4.959l3.007 2.332C4.672 5.164 6.656 3.58 9 3.58Z" />
              </svg>
              {busy ? 'CONNECTING…' : 'CONTINUE WITH GOOGLE'}
            </button>
            <div
              className="mono"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 10,
                marginTop: 18,
                color: 'var(--muted)',
                fontSize: 9.5,
                letterSpacing: '0.12em',
              }}
            >
              <span style={{ height: 1, background: 'var(--line)', flex: 1 }} />
              OR CONTINUE WITH EMAIL
              <span style={{ height: 1, background: 'var(--line)', flex: 1 }} />
            </div>
            <label className="micro-label" htmlFor="login-email" style={{ display: 'block', marginTop: 22 }}>
              Email address
            </label>
            <div className="composer" style={{ marginTop: 26 }}>
              <input
                id="login-email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void (usePassword ? signInPassword() : sendLink());
                }}
                placeholder="you@university.edu"
                required
                aria-invalid={error?.toLowerCase().includes('email') || undefined}
                aria-describedby={error ? 'login-error' : undefined}
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
                  id="login-password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') void signInPassword();
                  }}
                  placeholder="password"
                  aria-label="Password"
                  required
                  aria-invalid={error?.toLowerCase().includes('password') || undefined}
                  aria-describedby={error ? 'login-error' : undefined}
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
              <div id="login-error" role="alert" style={{ marginTop: 12, fontSize: 12, color: 'var(--terracotta)' }}>
                {error}
              </div>
            )}
          </>
        )}
        </div>
      </div>
    </div>
  );
}
