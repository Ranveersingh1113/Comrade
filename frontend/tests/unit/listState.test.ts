import { describe, expect, it } from 'vitest';
import { isConfirmedEmpty, listState } from '../../src/lib/listState';

describe('listState', () => {
  it('reports an empty list only when the server said so', () => {
    expect(listState(null, false, 0)).toBe('empty');
  });

  it('never calls a failed load empty', () => {
    // The defect this module exists for: every list screen initialised its
    // data to [] and added an error banner on failure, so a failed load
    // rendered "No tasks yet" and "Queue clear" next to the error.
    expect(listState('network down', false, 0)).toBe('error');
    expect(isConfirmedEmpty('network down', false, 0)).toBe(false);
  });

  it('never calls a load-in-progress empty', () => {
    expect(listState(null, true, 0)).toBe('loading');
    expect(isConfirmedEmpty(null, true, 0)).toBe(false);
  });

  it('prefers the error over stale rows', () => {
    // Rows from a previous successful load beside a failed refresh look
    // current and are not. An honest error beats confident staleness.
    expect(listState('refresh failed', false, 5)).toBe('error');
  });

  it('reports ready when there are rows and nothing went wrong', () => {
    expect(listState(null, false, 3)).toBe('ready');
  });

  it('treats an empty-string error as no error', () => {
    // Screens set '' as often as null when clearing.
    expect(listState('', false, 0)).toBe('empty');
  });

  it('treats undefined error as no error', () => {
    expect(listState(undefined, false, 2)).toBe('ready');
  });
});
