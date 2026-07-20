import { createClient } from '@supabase/supabase-js';

const url = import.meta.env.VITE_SUPABASE_URL as string | undefined;
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY as string | undefined;

if (!url || !anonKey) {
  // Fail fast and loudly: the app is unusable without these.
  throw new Error(
    'Missing VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY — copy .env.example to .env.local and fill it in.',
  );
}

// The anon key + the logged-in user's JWT is the ONLY client identity.
// All permissions are enforced by RLS server-side; never add service_role here.
export const supabase = createClient(url, anonKey);
