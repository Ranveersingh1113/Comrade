// Group-room view-model: message classification + contribution bars.
import type { Message, Profile, Task } from './types';

export type MessageKind = 'deleted' | 'ai' | 'user';

export interface ClassifiedMessage {
  kind: MessageKind;
  isDiffCard: boolean;
}

/** Deletion always renders as a trace — never a silent gap — so it wins.
 * diffCardIds = message ids referenced by memory_compilations.diff_message_id. */
export function classifyMessage(
  m: Message,
  diffCardIds: ReadonlySet<string>,
): ClassifiedMessage {
  if (m.deleted_scope === 'everyone') return { kind: 'deleted', isDiffCard: false };
  if (m.sender_kind === 'ai') return { kind: 'ai', isDiffCard: diffCardIds.has(m.id) };
  return { kind: 'user', isDiffCard: false };
}

export interface MemberBar {
  userId: string;
  name: string;
  tasks: Task[];
  doneCount: number;
}

export function memberBars(
  roster: ReadonlyArray<{ profile: Profile }>,
  tasks: ReadonlyArray<Task>,
): MemberBar[] {
  return roster.map(({ profile }) => {
    const mine = tasks.filter((t) => t.assignee_id === profile.id);
    return {
      userId: profile.id,
      name: profile.display_name,
      tasks: mine,
      doneCount: mine.filter((t) => t.status === 'done').length,
    };
  });
}
