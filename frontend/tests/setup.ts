import '@testing-library/jest-dom/vitest';
import { vi } from 'vitest';

// src/lib/supabase.ts throws without these; component tests mock the module,
// but anything importing it transitively still needs the env present.
vi.stubEnv('VITE_SUPABASE_URL', 'http://127.0.0.1:54321');
vi.stubEnv('VITE_SUPABASE_ANON_KEY', 'test-anon-key');
vi.stubEnv('VITE_AGENT_API_URL', 'http://localhost:8000');
