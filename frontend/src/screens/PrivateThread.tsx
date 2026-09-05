import { LegacyThreadRedirect } from './Threads';

/** Deprecated route kept only for callers that have not reached /threads yet. */
export function PrivateThread() {
  return <LegacyThreadRedirect privateThread />;
}
