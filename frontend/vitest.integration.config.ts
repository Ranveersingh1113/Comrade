import { defineConfig } from 'vitest/config';

// Real-stack layer: supabase-js as genuinely signed-in users against the
// local Supabase stack. Run with `npm run test:integration`.
export default defineConfig({
  test: {
    environment: 'node',
    include: ['tests/integration/**/*.test.ts'],
    globalSetup: ['./tests/integration/global-setup.ts'],
    testTimeout: 15000,
    hookTimeout: 30000,
    // team fixtures are cheap; serial keeps realtime tests deterministic
    fileParallelism: false,
  },
});
