/**
 * The breakpoint, and the reason there is exactly one.
 *
 * The product shipped with no breakpoints at all — `grep -rn "@media" src/`
 * returned nothing — while the sidebar was a hard `width: 250` and the group
 * room's rails `width: 296` and `width: 364`. At 375px the sidebar alone ate
 * two thirds of the viewport and the conversation was a sliver.
 */
import { describe, expect, it } from 'vitest';
import { NARROW_MAX, NARROW_QUERY, isNarrowWidth } from '../../src/lib/layout';

describe('layout breakpoint', () => {
  it('treats a phone as narrow', () => {
    expect(isNarrowWidth(375)).toBe(true);
    expect(isNarrowWidth(414)).toBe(true);
  });

  it('treats a tablet in portrait as narrow', () => {
    // 768 + a 250px sidebar + a 296px rail leaves 222px of conversation.
    // Side-by-side does not become usable until well past the tablet width.
    expect(isNarrowWidth(768)).toBe(true);
  });

  it('treats a laptop as wide', () => {
    expect(isNarrowWidth(1280)).toBe(false);
    expect(isNarrowWidth(NARROW_MAX + 1)).toBe(false);
  });

  it('is inclusive at the boundary', () => {
    expect(isNarrowWidth(NARROW_MAX)).toBe(true);
  });

  it('keeps the query string and the number in step', () => {
    // Two sources of truth for one breakpoint is a breakpoint that drifts:
    // the hook reads the query, a stylesheet might read the number.
    expect(NARROW_QUERY).toBe(`(max-width: ${NARROW_MAX}px)`);
  });

  it('only goes side-by-side once a readable column fits', () => {
    // The breakpoint's actual job. Side by side, the chrome is fixed:
    // 250 (sidebar) + 296 (rail) = 546px. So the NARROWEST WIDE viewport —
    // one pixel above the breakpoint — must still leave a readable column,
    // or the layout switches back to side-by-side too early and the
    // conversation is squeezed again at a width nobody tested.
    const CHROME = 250 + 296;
    const MIN_READABLE = 320;
    const narrowestWide = NARROW_MAX + 1;
    expect(narrowestWide - CHROME).toBeGreaterThanOrEqual(MIN_READABLE);
  });
});
