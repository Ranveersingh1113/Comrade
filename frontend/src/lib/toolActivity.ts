// What the member is told the agent is doing, while it does it.
//
// The runtime emits a tool_call frame per tool (agent/runtime.py), and until
// D6 both chat screens wasted them: GroupRoom showed three blinking dots and
// dropped the frames, PrivateThread printed the raw identifier — "checking
// team_get_state…". A spinner is not just slower-feeling than a real line, it
// is less honest: it says "wait" where the runtime is already saying exactly
// what it is doing.
//
// Deliberately NOT synced with agent/registry.py. A tool nobody labelled falls
// through to `using <name>`, which is true, specific, and matches the product's
// habit of showing its literal tool — so drift degrades the wording rather than
// the honesty, and there is nothing here worth a build step to guarantee.

const LABELS: Record<string, string> = {
  // Reads. RLS scopes these to the member already, so they are unremarkable —
  // the point of naming them is that "reading the wiki" is a better answer to
  // "why is this taking a moment" than a spinner.
  team_get_state: 'reading the room',
  memory_read_page: 'reading the wiki',
  messages_search: 'searching the room',
  document_read: 'reading a document',
  task_get: 'checking tasks',
  member_activity: 'checking who has been active',
  repo_activity: 'checking the repo',
  now: 'checking the date',

  // Proposals. The wording carries the consent model: nothing has happened
  // yet, and the member's key is what makes it happen. "creating a task"
  // would be a lie told at exactly the wrong moment.
  team_propose_task: 'drafting a task for your approval',
  task_propose_update: 'drafting a task change for your approval',

  // The agent's one ungated write (findings §9). It happens whether or not
  // this line renders, which is the reason the line should render.
  member_send_nudge: 'sending a private nudge',
};

/** A human sentence fragment for one tool call. Never empty. */
export function activityLabel(tool: string): string {
  return LABELS[tool] ?? `using ${tool}`;
}
