/**
 * Where the layout stops being a desktop layout.
 *
 * The product shipped with no breakpoints at all — `grep -rn "@media" src/`
 * returned nothing — while the sidebar was a hard `width: 250` and the group
 * room's rails `width: 296` and `width: 364`. At 375px the sidebar alone ate
 * two thirds of the viewport and the conversation was a sliver.
 *
 * One breakpoint, not a scale. Comrade has two layouts: everything side by
 * side, and everything stacked with the navigation tucked away. A tablet tier
 * would be a third thing to keep correct for no behaviour anyone asked for.
 */

/** Below this the sidebar goes off-canvas and rails stack. */
export const NARROW_MAX = 900;

export function isNarrowWidth(width: number): boolean {
  return width <= NARROW_MAX;
}

/**
 * The media query string, exported so the hook and any CSS stay in step.
 * A literal repeated in two places is a breakpoint that drifts.
 */
export const NARROW_QUERY = `(max-width: ${NARROW_MAX}px)`;
