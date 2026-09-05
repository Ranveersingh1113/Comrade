/**
 * What a list region should show: it failed, it is loading, it is genuinely
 * empty, or it has rows.
 *
 * The bug this exists to stop. Every list screen initialises its data to `[]`
 * and only *adds* an error banner on failure, so a failed load rendered the
 * error AND the empty state together:
 *
 *   Tasks         "No tasks yet."
 *
 * Both are POSITIVE ASSERTIONS about the world, and both are false when the
 * request simply did not come back. A member who half-reads a red banner and
 * fully reads a reassuring empty-state walks away reassured and wrong.
 *
 * "Empty" must mean "the server told us there is nothing", never "we do not
 * know".
 */
export type ListState = 'error' | 'loading' | 'empty' | 'ready';

export function listState(
  error: string | null | undefined,
  loading: boolean,
  count: number,
): ListState {
  // Error wins over everything, including rows: stale rows beside a failed
  // refresh are worse than an honest error, because they look current.
  if (error) return 'error';
  if (loading) return 'loading';
  return count === 0 ? 'empty' : 'ready';
}

/** True only when the server has actually confirmed there is nothing here. */
export function isConfirmedEmpty(
  error: string | null | undefined,
  loading: boolean,
  count: number,
): boolean {
  return listState(error, loading, count) === 'empty';
}
