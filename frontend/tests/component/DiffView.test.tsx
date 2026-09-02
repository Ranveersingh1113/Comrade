import { describe, expect, test } from 'vitest';
import { render, screen } from '@testing-library/react';

import { DiffView } from '../../src/components/DiffView';

const PATCH = [
  'diff --git a/src/auth.py b/src/auth.py',
  'index 1111111..2222222 100644',
  '--- a/src/auth.py',
  '+++ b/src/auth.py',
  '@@ -10,7 +10,7 @@ def check(token):',
  '     if not token:',
  '-        return True',
  '+        return False',
  '     return verify(token)',
].join('\n');

describe('DiffView', () => {
  test('renders each line of the diff on its own line', () => {
    // 🔴 The bug this replaced. ConsentCard rendered every tool argument as
    // `{key} {value}`, so a patch arrived as ONE unwrapped line containing
    // literal backslash-n characters: present, and unreadable. A reviewer who
    // cannot see the change is not reviewing it — they are clicking approve
    // because the agent seems confident, which is the failure consent exists
    // to prevent.
    const { container } = render(<DiffView patch={PATCH} />);
    const rendered = container.querySelectorAll('pre > div');
    expect(rendered.length).toBe(PATCH.split('\n').length);
    // Exact text, INCLUDING the leading whitespace. getByText collapses runs
    // of spaces, and indentation is precisely what a reviewer of a Python
    // diff needs to see — so this reads the nodes directly rather than
    // through a matcher that would hide a lost indent.
    const text = [...rendered].map((n) => n.textContent);
    expect(text).toContain('-        return True');
    expect(text).toContain('+        return False');
  });

  test('summarises what is being approved before the reader scrolls', () => {
    render(<DiffView patch={PATCH} />);
    // One file, one added line, one removed. +++ and --- are file headers and
    // must not be counted as content, which is the easy off-by-two here.
    expect(screen.getByText('+1')).toBeTruthy();
    expect(screen.getByText('−1')).toBeTruthy();
    expect(screen.getByText(/1 file\b/)).toBeTruthy();
  });

  test('a very long diff says what it is not showing', () => {
    // Silently rendering a prefix would let someone approve a change whose
    // second half they never saw, believing they had read all of it.
    const long = Array.from({ length: 900 }, (_, i) => `+line ${i}`).join('\n');
    render(<DiffView patch={long} />);
    expect(screen.getByText(/more lines/)).toBeTruthy();
  });

  test('an empty line still occupies a row', () => {
    // A blank context line in a diff is meaningful — collapsing it shifts
    // everything after it and makes the change look like it touches different
    // code than it does.
    const { container } = render(<DiffView patch={'@@ -1 +1 @@\n\n+x'} />);
    expect(container.querySelectorAll('pre > div').length).toBe(3);
  });
});
