import { useTranslation } from 'react-i18next';
import { ChevronRight } from 'lucide-react';
import { Button } from '@/components/ui/button';

/**
 * How many tasks run, as one quiet line above the composer.
 *
 * Most of the time a reader only needs to know that work runs apart from the
 * reply, so the line says how much — the way Codex keeps its background
 * terminals to a single dim footer row — and opens the Tasks panel, where
 * each task is listed and stopped, as Claude Code's `/tasks` is.
 */
export function SessionTasksLine({ count, onOpen }: { count: number; onOpen: () => void }) {
  const { t } = useTranslation();
  if (count === 0) return null;
  return (
    <div className="mb-1 flex" role="status" data-testid="session-tasks-line">
      <Button
        variant="ghost"
        size="xs"
        className="font-normal text-muted-foreground hover:text-foreground"
        data-testid="session-tasks-summary"
        onClick={onOpen}
      >
        {t('chat:tasks.summary', { count })}
        <ChevronRight aria-hidden="true" data-icon="inline-end" />
      </Button>
    </div>
  );
}
