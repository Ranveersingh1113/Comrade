import { defineConfig } from 'vitest/config';

// Real-stack layer: supabase-js as genuinely signed-in users against the
// local Supabase stack. Run with `npm run test:integration`.
export default defineConfig({
  test: {
    environment: 'node',
    include: ['tests/integration/**/*.test.ts'],
    globalSetup: ['./tests/integration/global-setup.ts'],
    // must exceed realtime.test.ts's EVENT_WAIT_MS ceiling, or vitest kills
    // the test before its own bound reports a useful message
    testTimeout: 20000,
    hookTimeout: 30000,
    // team fixtures are cheap; serial keeps realtime tests deterministic
    fileParallelism: false,
  },
});
