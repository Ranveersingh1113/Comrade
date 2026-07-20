import { useCallback, useEffect, useState } from 'react';
import { supabase } from '../lib/supabase';
import type { Task, TaskStatus } from '../lib/types';
import { useTeam } from '../state/TeamContext';
import { useTeamRealtime } from './useRealtime';

const NEXT: Partial<Record<TaskStatus, TaskStatus>> = {
  proposed: 'confirmed',
  confirmed: 'in_progress',
  in_progress: 'done',
};

export interface TaskActions {
  tasks: Task[];
  loading: boolean;
  error: string | null;
  /** Advance one step. The DB trigger enforces assignee-only confirm; the UI
   * mirrors it (showConfirm/showWait) rather than re-implementing policy. */
  advance: (task: Task) => Promise<void>;
  create: (title: string, assigneeId: string | null, deadline: string | null) => Promise<void>;
  refresh: () => Promise<void>;
}

export function useTasks(): TaskActions {
  const { team, myUserId } = useTeam();
  const teamId = team?.id ?? '';
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!teamId) return;
    const { data, error: err } = await supabase
      .from('tasks')
      .select('*')
      .eq('team_id', teamId)
      .order('created_at');
    if (err) setError(err.message);
    else setTasks((data as Task[] | null) ?? []);
    setLoading(false);
  }, [teamId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  useTeamRealtime('tasks', teamId, refresh);

  const advance = useCallback(
    async (task: Task) => {
      const next = NEXT[task.status];
      if (!next) return;
      const patch: Partial<Task> = { status: next };
      if (next === 'confirmed') patch.confirmed_at = new Date().toISOString();
      const { error: err } = await supabase.from('tasks').update(patch).eq('id', task.id);
      if (err) {
        // e.g. "only the assignee may confirm their own task" from the trigger
        setError(err.message);
        return;
      }
      setError(null);
      await refresh();
    },
    [refresh],
  );

  const create = useCallback(
    async (title: string, assigneeId: string | null, deadline: string | null) => {
      const { error: err } = await supabase.from('tasks').insert({
        team_id: teamId,
        title,
        assignee_id: assigneeId,
        deadline,
        status: 'proposed',
        created_by_kind: 'user',
        created_by_id: myUserId,
      });
      if (err) setError(err.message);
      else {
        setError(null);
        await refresh();
      }
    },
    [teamId, myUserId, refresh],
  );

  return { tasks, loading, error, advance, create, refresh };
}

/** Prototype's task-cell styling, keyed by status. */
export function taskCell(status: TaskStatus) {
  return {
    proposed: { bg: 'transparent', border: 'rgba(35,33,48,0.35)', style: 'dashed' },
    confirmed: { bg: 'transparent', border: 'rgba(35,33,48,0.5)', style: 'solid' },
    in_progress: { bg: '#F0A28A', border: '#E4795B', style: 'solid' },
    done: { bg: '#D2593B', border: '#D2593B', style: 'solid' },
  }[status];
}

export function taskPill(status: TaskStatus) {
  return {
    proposed: { label: 'PROPOSED', color: '#A9A5B0', border: 'rgba(169,165,176,0.5)' },
    confirmed: { label: 'CONFIRMED', color: '#6E5F87', border: 'rgba(110,95,135,0.5)' },
    in_progress: { label: 'IN PROGRESS', color: '#E4795B', border: 'rgba(228,121,91,0.5)' },
    done: { label: 'DONE', color: '#D2593B', border: 'rgba(210,89,59,0.5)' },
  }[status];
}

export function taskMark(status: TaskStatus): string {
  if (status === 'done') return '✓';
  if (status === 'in_progress') return '·';
  return '';
}
