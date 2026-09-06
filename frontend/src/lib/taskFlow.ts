// Task lifecycle view-model. The DB trigger is the authority on who may
// confirm; this module MIRRORS it for rendering, never replaces it.
import type { Task, TaskStatus } from './types';

const NEXT: Partial<Record<TaskStatus, TaskStatus>> = {
  proposed: 'confirmed',
  confirmed: 'in_progress',
  in_progress: 'done',
};

export function nextStatus(status: TaskStatus): TaskStatus | null {
  return NEXT[status] ?? null;
}

export function confirmPatch(next: TaskStatus): Partial<Task> {
  const patch: Partial<Task> = { status: next };
  if (next === 'confirmed') patch.confirmed_at = new Date().toISOString();
  return patch;
}

export type TaskAffordance =
  | { kind: 'confirm' }
  | { kind: 'start' }
  | { kind: 'finish' }
  | { kind: 'wait'; assigneeName: string }
  | { kind: 'none' };

/** Which control the viewer gets for a task. */
export function taskAffordance(
  task: Pick<Task, 'status' | 'assignee_id'>,
  viewerId: string | null,
  assigneeName: string,
): TaskAffordance {
  const mine = viewerId !== null && task.assignee_id === viewerId;
  if (mine) {
    if (task.status === 'proposed') return { kind: 'confirm' };
    if (task.status === 'confirmed') return { kind: 'start' };
    if (task.status === 'in_progress') return { kind: 'finish' };
    return { kind: 'none' };
  }
  if (task.status === 'proposed') return { kind: 'wait', assigneeName };
  return { kind: 'none' };
}

/** Prototype's task-cell styling, keyed by status. */
export function taskCell(status: TaskStatus) {
  return {
    proposed: { bg: 'transparent', border: 'rgba(35,33,48,0.35)', style: 'dashed' },
    confirmed: { bg: 'transparent', border: 'rgba(35,33,48,0.5)', style: 'solid' },
    in_progress: { bg: '#efad9a', border: '#c76863', style: 'solid' },
    done: { bg: '#c76863', border: '#c76863', style: 'solid' },
  }[status];
}

export function taskPill(status: TaskStatus) {
  return {
    proposed: { label: 'PROPOSED', color: '#A9A5B0', border: 'rgba(169,165,176,0.5)' },
    confirmed: { label: 'CONFIRMED', color: '#6E5F87', border: 'rgba(110,95,135,0.5)' },
    in_progress: { label: 'IN PROGRESS', color: '#9c5b68', border: 'rgba(156,91,104,0.5)' },
    done: { label: 'DONE', color: '#765579', border: 'rgba(118,85,121,0.5)' },
  }[status];
}

export function taskMark(status: TaskStatus): string {
  if (status === 'done') return '✓';
  if (status === 'in_progress') return '·';
  return '';
}
