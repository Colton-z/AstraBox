import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ErrorNote } from '@/components/shell/ErrorNote';
import { Button } from '@/components/ui/button';
import type { SessionRecord } from '../../types';

type BackgroundTask = NonNullable<SessionRecord['background_tasks']>[number];

/**
 * Work the Agent keeps running in its box after its reply ended, such as a
 * background shell: listed apart from the conversation, each stoppable on its
 * own. A stopped task leaves the list when the runtime reports that it ended.
 */
export function SessionBackgroundTasks({ tasks, onStop }: {
  tasks: BackgroundTask[];
  onStop: (taskId: string) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [stopping, setStopping] = useState<ReadonlySet<string>>(new Set());
  const [error, setError] = useState('');
  if (!tasks.length) return null;
  const stop = async (taskId: string) => {
    setError('');
    setStopping((current) => new Set(current).add(taskId));
    try {
      await onStop(taskId);
    } catch (cause) {
      setStopping((current) => new Set([...current].filter((id) => id !== taskId)));
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  };
  return (
    <div className="mb-3 space-y-2 rounded-lg border p-3 text-sm" role="status" data-testid="session-background-tasks">
      <p className="font-medium">{t('chat:background_tasks.title')}</p>
      <p className="text-muted-foreground">{t('chat:background_tasks.hint')}</p>
      <ul className="space-y-2">
        {tasks.map((task) => (
          <li key={task.task_id} className="flex items-center gap-2" data-testid="session-background-task">
            <span className="min-w-0 flex-1 truncate" title={task.description || task.task_id}>
              {task.description || task.task_id}
            </span>
            {task.kind && <span className="shrink-0 text-muted-foreground">{task.kind}</span>}
            <Button size="sm" variant="outline" className="shrink-0" disabled={stopping.has(task.task_id)}
              onClick={() => void stop(task.task_id)}>
              {stopping.has(task.task_id) ? t('chat:background_tasks.stopping') : t('chat:background_tasks.stop')}
            </Button>
          </li>
        ))}
      </ul>
      {error && <ErrorNote>{error}</ErrorNote>}
    </div>
  );
}
