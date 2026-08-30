/**
 * Loads env for the integration layer and probes the local stack once.
 * Tests skip (loudly) rather than fail when the stack is down.
 */
import { config as loadEnv } from 'dotenv';
import { resolve } from 'node:path';

export default function setup(): void {
  // repo-root .env: COMRADE_DB_URL_ADMIN. frontend/.env.local: VITE_* keys.
  loadEnv({ path: resolve(__dirname, '../../../.env'), quiet: true });
  loadEnv({ path: resolve(__dirname, '../../.env.local'), quiet: true });

  if (!process.env.VITE_SUPABASE_URL || !process.env.COMRADE_DB_URL_ADMIN) {
    console.warn(
      '[integration] missing VITE_SUPABASE_URL or COMRADE_DB_URL_ADMIN — all suites will skip',
    );
  }
}
