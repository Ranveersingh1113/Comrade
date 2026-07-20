import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, vi } from 'vitest';

// RTL's auto-cleanup only hooks a GLOBAL afterEach; vitest here runs without
// globals, so renders would pile up across tests without this.
afterEach(cleanup);

// src/lib/supabase.ts throws without these; component tests mock the module,
// but anything importing it transitively still needs the env present.
vi.stubEnv('VITE_SUPABASE_URL', 'http://127.0.0.1:54321');
vi.stubEnv('VITE_SUPABASE_ANON_KEY', 'test-anon-key');
vi.stubEnv('VITE_AGENT_API_URL', 'http://localhost:8000');
