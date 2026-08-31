/**
 * The three outcomes of one button.
 *
 * Leaving looks like a single action and is not: the last member out archives
 * the team, and a departing leader hands the role on. Both happen in
 * trg_membership_departure_effects, invisibly, so the copy above the confirm
 * button is the only place a member learns about them.
 *
 * This file exists to keep that copy honest. If the trigger's branches change,
 * one of these goes red.
 */
import { describe, expect, it } from 'vitest';
import { leaveConsequenceText, leaveOutcome } from '../../src/lib/leaving';

describe('leaveOutcome', () => {
  it('archives the team when the last member leaves', () => {
    expect(leaveOutcome({ isLeader: false, activeCount: 1 })).toBe('archives-team');
  });

  it('prefers "archives" over "passes leadership" for a lone leader', () => {
    // The trigger checks `still_active` before promoting anyone, so a leader
    // who is also the last member archives the team and promotes nobody.
    // Getting this backwards would promise a handover to an empty team.
    expect(leaveOutcome({ isLeader: true, activeCount: 1 })).toBe('archives-team');
  });

  it('passes leadership when the leader leaves others behind', () => {
    expect(leaveOutcome({ isLeader: true, activeCount: 3 })).toBe('passes-leadership');
  });

  it('changes nothing else for an ordinary member', () => {
    expect(leaveOutcome({ isLeader: false, activeCount: 3 })).toBe('nothing-else');
  });

  it('treats an impossible count as the last member rather than crashing', () => {
    // activeCount comes from a roster that may not have loaded. Rounding an
    // unknown down to the most consequential warning is the safe direction:
    // over-warning costs a sentence, under-warning closes a team silently.
    expect(leaveOutcome({ isLeader: false, activeCount: 0 })).toBe('archives-team');
  });
});

describe('leaveConsequenceText', () => {
  it('tells the last member the team closes, and to export first', () => {
    const text = leaveConsequenceText({ isLeader: true, activeCount: 1 });
    expect(text).toMatch(/closes the team/);
    expect(text).toMatch(/Export first/);
  });

  it('names the handover so a leader is not surprised by it', () => {
    expect(leaveConsequenceText({ isLeader: true, activeCount: 2 })).toMatch(
      /passes that to whoever has been here longest/,
    );
  });

  it('says what survives, because "left" is not a deletion', () => {
    expect(leaveConsequenceText({ isLeader: false, activeCount: 4 })).toMatch(
      /messages and tasks stay/,
    );
  });
});
