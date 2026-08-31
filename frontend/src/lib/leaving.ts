// What actually happens when you leave, said before you click.
//
// Leaving is one button with three quite different outcomes, and two of them
// are invisible from the button: the last member out ARCHIVES the team, and a
// departing leader hands the role to the longest-standing member. Both are
// done by trg_membership_departure_effects (20260901100000), so a member who
// is not told will find out afterwards or not at all.
//
// This mirrors that trigger deliberately. If the SQL branches change, this has
// to change with it — tests/unit/leaving.test.ts states the pairing, and the
// migration is the source of truth for both.

export interface LeaveContext {
  /** Is the person leaving currently the team's leader? */
  isLeader: boolean;
  /** Active members INCLUDING the person leaving. */
  activeCount: number;
}

export type LeaveOutcome = 'archives-team' | 'passes-leadership' | 'nothing-else';

export function leaveOutcome({ isLeader, activeCount }: LeaveContext): LeaveOutcome {
  // Checked first: an emptied team has nobody to promote, so "last member out"
  // wins over "leader leaving" when the leader IS the last member. The trigger
  // orders it the same way, and getting this backwards would promise a
  // handover to a team with nobody left in it.
  if (activeCount <= 1) return 'archives-team';
  if (isLeader) return 'passes-leadership';
  return 'nothing-else';
}

/** The sentence shown above the confirm button. Factual, no scolding. */
export function leaveConsequenceText(ctx: LeaveContext): string {
  switch (leaveOutcome(ctx)) {
    case 'archives-team':
      return (
        'You are the last member. Leaving closes the team — its history is kept' +
        ' but nobody will be able to open it. Export first if you want a copy.'
      );
    case 'passes-leadership':
      return (
        'You are the team lead. Leaving passes that to whoever has been here' +
        ' longest, so the team can still invite and rename.'
      );
    case 'nothing-else':
      return 'Your messages and tasks stay; your access ends.';
  }
}
