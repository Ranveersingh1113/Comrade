import { useEffect, useState } from 'react';
import { NARROW_QUERY } from '../lib/layout';

/**
 * True while the viewport is narrow enough that the side-by-side layout does
 * not fit. Driven by matchMedia rather than a resize listener: the browser
 * already knows when the answer changes, and polling window.innerWidth on
 * every resize event is work for a boolean that flips twice a session.
 *
 * Guarded for environments without matchMedia — jsdom in the component tests
 * does not implement it — so a missing implementation reads as "wide" rather
 * than crashing the render.
 */
export function useIsNarrow(): boolean {
  const [narrow, setNarrow] = useState(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return false;
    return window.matchMedia(NARROW_QUERY).matches;
  });

  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return;
    const mq = window.matchMedia(NARROW_QUERY);
    const onChange = (e: MediaQueryListEvent) => setNarrow(e.matches);
    setNarrow(mq.matches);
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);

  return narrow;
}
