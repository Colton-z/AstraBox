import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronUp } from 'lucide-react';
import { ErrorNote } from '@/components/shell/ErrorNote';
import { Button } from '@/components/ui/button';
import {
  Popover,
  PopoverContent,
  PopoverDescription,
  PopoverHeader,
  PopoverTitle,
  PopoverTrigger,
} from '@/components/ui/popover';
import type { SessionRecord } from '../../types';

type BackgroundTask = NonNullable<SessionRecord['background_tasks']>[number];

/**
 * Work the Agent keeps running in its box after its reply ended, such as a
 * background shell.
 *
 * Most of the time a reader only needs to know that it is there, so it reads
 * as one quiet line above the composer — how many tasks run — the way Codex
 * keeps its background terminals to a single dim footer row and lists them
 * only on request. The line opens the list, where each task stops on its own.
 * A stopped task leaves the list when the runtime reports that it ended.
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
    <div className="mb-1 flex" role="status" data-testid="session-background-tasks">
      <Popover>
        <PopoverTrigger
          render={(
            <Button
              variant="ghost"
              size="xs"
              className="font-normal text-muted-foreground hover:text-foreground"
              data-testid="session-background-tasks-summary"
            />
          )}
        >
          {t('chat:background_tasks.summary', { count: tasks.length })}
          <ChevronUp aria-hidden="true" />
        </PopoverTrigger>
        <PopoverContent side="top" align="start" className="w-96 max-w-[calc(100vw-2rem)]">
          <PopoverHeader>
            <PopoverTitle>{t('chat:background_tasks.title')}</PopoverTitle>
            <PopoverDescription>{t('chat:background_tasks.hint')}</PopoverDescription>
          </PopoverHeader>
          <ul className="flex flex-col gap-2">
            {tasks.map((task) => (
              <li key={task.task_id} className="flex items-center gap-2" data-testid="session-background-task">
                <span className="min-w-0 flex-1 truncate" title={task.description || task.task_id}>
                  {task.description || task.task_id}
                </span>
                {task.kind && <span className="shrink-0 text-xs text-muted-foreground">{task.kind}</span>}
                <Button size="sm" variant="outline" className="shrink-0" disabled={stopping.has(task.task_id)}
                  onClick={() => void stop(task.task_id)}>
                  {stopping.has(task.task_id) ? t('chat:background_tasks.stopping') : t('chat:background_tasks.stop')}
                </Button>
              </li>
            ))}
          </ul>
          {error && <ErrorNote>{error}</ErrorNote>}
        </PopoverContent>
      </Popover>
    </div>
  );
}
