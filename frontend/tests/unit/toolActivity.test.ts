/**
 * What the member is told the agent is doing.
 *
 * The runtime emits a tool_call frame for every tool the model invokes, and
 * both chat screens were throwing that away or printing the raw identifier —
 * GroupRoom showed three blinking dots and nothing else, PrivateThread showed
 * "checking team_get_state…".
 *
 * The interesting case is the LAST one: an unlabelled tool. Naming it is the
 * honest fallback, and it is what makes this map safe to leave un-synced with
 * agent/registry.py — a tool nobody labelled degrades to something true rather
 * than to a lie or a blank.
 */
import { describe, expect, it } from 'vitest';
import { activityLabel } from '../../src/lib/toolActivity';

describe('activityLabel', () => {
  it('says what a read is actually reading', () => {
    expect(activityLabel('memory_read_page')).toBe('reading the wiki');
    expect(activityLabel('messages_search')).toBe('searching the room');
    expect(activityLabel('document_read')).toBe('reading a document');
  });

  it('says a proposal is only a draft', () => {
    // The member has not approved anything yet, and the line must not imply
    // the task exists — that is the whole consent model, in four words.
    expect(activityLabel('team_propose_task')).toMatch(/^drafting/);
    expect(activityLabel('team_propose_task')).toMatch(/for your approval$/);
    expect(activityLabel('task_propose_update')).toMatch(/for your approval$/);
    expect(activityLabel('team_propose_batch')).toMatch(/for your approval$/);
  });

  it('does not hide the one tool that acts immediately', () => {
    // member_send_nudge is the agent's single ungated write (findings §9).
    // If anything gets a visible line, it is that one.
    expect(activityLabel('member_send_nudge')).toBe('sending a private nudge');
  });

  it('names a tool it has no label for', () => {
    // Truthful and specific beats a generic "working…". It is also why this
    // map does not need a drift test against agent/registry.py: a new tool
    // reads slightly worse, never wrong.
    expect(activityLabel('some_future_tool')).toBe('using some_future_tool');
  });

  it('never returns an empty string', () => {
    // The caller renders this directly; '' would silently collapse the line
    // back to the blank spinner this module exists to replace.
    for (const name of ['', 'x', 'memory_read_page', 'unknown_thing']) {
      expect(activityLabel(name).length).toBeGreaterThan(0);
    }
  });
});
