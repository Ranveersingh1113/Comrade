import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import type { Session } from '@supabase/supabase-js';
import { supabase } from '../lib/supabase';
import type { Profile } from '../lib/types';

interface AuthState {
  session: Session | null;
  profile: Profile | null;
  loading: boolean;
  error: string | null;
  retry: () => void;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthState>({
  session: null,
  profile: null,
  loading: true,
  error: null,
  retry: () => {},
  signOut: async () => {},
});

/** What to call someone, from whatever the identity provider gave us.
 *
 * Each provider names this field differently, and the fallback is what a
 * teammate SEES next to every message — so getting it wrong is not cosmetic.
 * Google returns `full_name` and `name` and never `display_name`, so an
 * account created through the Google button would have been called
 * "priya.sharma" from the email prefix while the same person signing up with a
 * password got "Priya Sharma".
 *
 * Exported because it is the whole decision, and a pure function is testable
 * without standing up a session.
 */
export function displayNameFrom(user: Session['user']): string {
  const meta = (user.user_metadata ?? {}) as Record<string, unknown>;
  for (const key of ['display_name', 'full_name', 'name']) {
    const value = meta[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return user.email?.split('@')[0] || 'Member';
}

/** Ensure a profiles row exists for the signed-in user (RLS: insert self only). */
async function ensureProfile(session: Session): Promise<Profile | null> {
  const uid = session.user.id;
  const { data: existing } = await supabase
    .from('profiles')
    .select('*')
    .eq('id', uid)
    .maybeSingle();
  if (existing) return existing as Profile;

  const displayName = displayNameFrom(session.user);
  const { data: created, error } = await supabase
    .from('profiles')
    .insert({ id: uid, display_name: displayName, email: session.user.email })
    .select()
    .single();
  if (error) {
    console.error('profile bootstrap failed', error);
    return null;
  }
  return created as Profile;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retryNonce, setRetryNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    supabase.auth
      .getSession()
      .then(({ data }) => {
        if (cancelled) return;
        setSession(data.session);
        if (!data.session) setLoading(false);
      })
      .catch(() => {
        if (cancelled) return;
        setSession(null);
        setProfile(null);
        setError('We could not verify your session. Check your connection and try again.');
        setLoading(false);
      });

    const { data: sub } = supabase.auth.onAuthStateChange((_event, next) => {
      setSession(next);
      if (!next) {
        setProfile(null);
        setLoading(false);
      }
    });
    return () => {
      cancelled = true;
      sub.subscription.unsubscribe();
    };
  }, [retryNonce]);

  useEffect(() => {
    if (!session) return;
    let cancelled = false;
    ensureProfile(session).then((p) => {
      if (cancelled) return;
      setProfile(p);
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [session?.user.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const signOut = async () => {
    await supabase.auth.signOut();
  };

  const retry = () => setRetryNonce((value) => value + 1);

  return (
    <AuthContext.Provider value={{ session, profile, loading, error, retry, signOut }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  return useContext(AuthContext);
}
