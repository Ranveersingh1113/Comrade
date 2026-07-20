import { useCallback, useEffect, useState } from 'react';
import { supabase } from '../lib/supabase';
import { confirmPatch, nextStatus } from '../lib/taskFlow';
import type { Task } from '../lib/types';
import { useTeam } from '../state/TeamContext';
import { useTeamRealtime } from './useRealtime';

// Presentation maps moved to lib/taskFlow (pure, unit-tested); re-exported
// so existing imports keep working.
export { taskCell, taskMark, taskPill } from '../lib/taskFlow';

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
      const next = nextStatus(task.status);
      if (!next) return;
      const patch = confirmPatch(next);
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
