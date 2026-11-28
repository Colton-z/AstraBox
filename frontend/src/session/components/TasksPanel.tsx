import { useId, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

import { EmptyState, ErrorNote } from '@/components/shell';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Item,
  ItemActions,
  ItemContent,
  ItemDescription,
  ItemFooter,
  ItemHeader,
  ItemMedia,
  ItemTitle,
} from '@/components/ui/item';
import { ScrollArea } from '@/components/ui/scroll-area';
import { cn } from '@/lib/utils';
import { BotIcon, CircleIcon, ListChecksIcon, LoaderIcon, SquareTerminalIcon } from 'lucide-react';
import type { BackgroundTask, SessionTasks } from '../hooks/useSessionTasks';
import { subagentDisplayStatus, type SubagentEntry } from '../hooks/useSubagentRegistry';

interface TasksPanelProps {
  tasks: SessionTasks;
  selectedChildRunId: string | null;
  onSelect: (childRunId: string | null) => void;
  onStopCommand: (taskId: string) => Promise<void>;
  projectionError?: string | null;
}

function StatusIcon({ active }: { active: boolean }) {
  if (active) {
    return <LoaderIcon className="size-3.5 animate-spin text-primary" />;
  }
  return <CircleIcon className="size-3.5 text-muted-foreground" />;
}

function shortId(id: string): string {
  return id.length <= 8 ? id : id.slice(0, 8);
}

function formatDuration(ms?: number): string | null {
  if (ms === undefined || ms === null || !Number.isFinite(ms)) return null;
  if (ms < 1000) return `${ms}ms`;
  const sec = ms / 1000;
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const min = Math.floor(sec / 60);
  return `${min}m${Math.round(sec - min * 60)}s`;
}

function UsageChip({ entry }: { entry: SubagentEntry }) {
  const usage = entry.usage ?? {};
  const parts: string[] = [];
  if (entry.model) parts.push(entry.model);
  // Skip `0 tok`: the SDK reports total_tokens=0 for local_agent subagents that
  // dispatch tools without invoking the LLM (tool_uses + duration_ms populate
  // but the token account is left at 0). Showing "0 tok" reads as a real
  // measurement and misleads users into thinking the subagent didn't run.
  if (typeof usage.total_tokens === 'number' && usage.total_tokens > 0) {
    parts.push(`${usage.total_tokens.toLocaleString()} tok`);
  }
  if (typeof usage.tool_uses === 'number' && usage.tool_uses > 0) {
    parts.push(`${usage.tool_uses} tools`);
  }
  const duration = formatDuration(usage.duration_ms);
  if (duration) parts.push(duration);
  if (parts.length === 0) return null;
  /*
    `ghost`, not `secondary`: the status Badge sits immediately to the left in
    the same footer, and a second pill on the same wash would read as one
    control split in two. Usage is metadata beside a state, so it carries no
    fill of its own.

    The hover wash is turned off because the whole row is a button: ghost's
    hover fires on the row's hover, painting a surface for an interaction that
    is not this chip's (§11). Both modifiers, since `dark:hover:` and `hover:`
    are separate keys to tailwind-merge.
  */
  return (
    <Badge
      variant="ghost"
      className="font-mono text-10 text-muted-foreground hover:bg-transparent dark:hover:bg-transparent"
    >
      {parts.join(' · ')}
    </Badge>
  );
}

export function AgentRow({
  entry,
  selected,
  onSelect,
}: {
  entry: SubagentEntry;
  selected: boolean;
  onSelect: (childRunId: string) => void;
}) {
  const title = entry.description?.trim() || `Subagent ${shortId(entry.childRunId)}`;
  const subtitle = entry.summary?.trim() || entry.lastToolName?.trim() || entry.taskType?.trim() || '';
  const status = subagentDisplayStatus(entry);
  const indent = Math.max(0, entry.depth - 1) * 12;
  /*
    The kit's `Item`, at the `xs` band: the two stacked rows this needs are
    `ItemHeader` and `ItemFooter`, which are `basis-full` inside a wrapping
    flex — the shape the component is built for rather than a second one
    written beside it. `xs` is what makes the description 12px and closes the
    gap between title and subtitle, so the sizes are read off the band (§9)
    instead of being spelled per element.

    It renders as a button. `Item` is a div by default and this row is pressed
    to open a transcript, so the element has to be the control (§10); `render`
    is how Base UI's `useRender` composes one without nesting two.
  */
  return (
    <Item
      variant="outline"
      size="xs"
      render={<button type="button" />}
      data-testid="subagent-agent-row"
      data-child-run-id={entry.childRunId}
      data-parent-child-run-id={entry.parentChildRunId ?? ''}
      data-subagent-depth={entry.depth}
      data-subagent-task-type={entry.taskType ?? ''}
      onClick={() => onSelect(entry.childRunId)}
      style={{ marginLeft: `${indent}px`, width: `calc(100% - ${indent}px)` }}
      className={cn(
        'min-w-0 overflow-hidden bg-card text-left hover:bg-accent',
        selected && 'border-primary bg-accent',
      )}
    >
      <ItemHeader className="min-w-0 items-start">
        <ItemMedia variant="icon" className="text-muted-foreground">
          <BotIcon />
        </ItemMedia>
        <ItemContent className="min-w-0">
          {/* `line-clamp-none`, so the title row is deterministically the flex
              `ItemTitle` declares. Line clamping sets `display:-webkit-box`,
              which competes with `flex` in the same class list and is settled
              by stylesheet order rather than by anything written here — and a
              box would drop the `flex-1` that makes the name give way to the
              id beside it. The name truncates on its own span. */}
          <ItemTitle className="w-full min-w-0 gap-1.5 line-clamp-none">
            <span className="min-w-0 flex-1 truncate" title={title}>
              {title}
            </span>
            {/* `text-muted-foreground` over the variant's own foreground: the
                id is metadata sitting beside the name, and secondary's
                full-strength ink would make it compete with the title. Mono
                stays — a run id is something you paste into a query (§6). */}
            <Badge variant="secondary" className="font-mono text-10 text-muted-foreground">
              {shortId(entry.childRunId)}
            </Badge>
          </ItemTitle>
          {subtitle && (
            <ItemDescription className="line-clamp-1 min-w-0" title={subtitle}>
              {subtitle}
            </ItemDescription>
          )}
        </ItemContent>
      </ItemHeader>
      <ItemFooter className="min-w-0 flex-wrap justify-start gap-1.5 pl-6">
        <StatusIcon active={entry.active} />
        <Badge variant={entry.active ? 'default' : 'outline'} className="h-5 shrink-0 text-10">
          {status}
        </Badge>
        <UsageChip entry={entry} />
      </ItemFooter>
    </Item>
  );
}

/** Background work with no child run: what it is, and a Stop of its own. */
function CommandRow({
  task,
  stopping,
  onStop,
}: {
  task: BackgroundTask;
  stopping: boolean;
  onStop: (taskId: string) => void;
}) {
  const { t } = useTranslation();
  const title = task.description || task.task_id;
  return (
    <Item
      variant="outline"
      size="xs"
      data-testid="session-task-command"
      data-task-id={task.task_id}
      className="min-w-0 flex-nowrap overflow-hidden bg-card"
    >
      <ItemMedia variant="icon" className="text-muted-foreground">
        <SquareTerminalIcon />
      </ItemMedia>
      <ItemContent className="min-w-0">
        <ItemTitle className="w-full min-w-0 line-clamp-none">
          <span className="min-w-0 truncate" title={title}>{title}</span>
        </ItemTitle>
        {task.kind && (
          <ItemDescription className="line-clamp-1 font-mono text-10">{task.kind}</ItemDescription>
        )}
      </ItemContent>
      <ItemActions>
        <Button size="xs" variant="outline" disabled={stopping} onClick={() => onStop(task.task_id)}>
          {stopping ? t('chat:tasks.stopping') : t('chat:tasks.stop')}
        </Button>
      </ItemActions>
    </Item>
  );
}

function TaskSection({
  title,
  testId,
  children,
}: {
  title: string;
  testId: string;
  children: ReactNode;
}) {
  const id = useId();
  return (
    <section aria-labelledby={id} data-testid={testId} className="flex min-w-0 flex-col gap-2">
      <h3 id={id} className="px-1 text-xs font-medium text-muted-foreground">{title}</h3>
      {children}
    </section>
  );
}

/**
 * The Session's work in one place: what runs now, shells and subagents
 * together, above the subagents that have finished. A subagent row opens its
 * transcript, where it stops; a command stops from its row.
 */
export function TasksPanel({
  tasks,
  selectedChildRunId,
  onSelect,
  onStopCommand,
  projectionError,
}: TasksPanelProps) {
  const { t } = useTranslation();
  const [stopping, setStopping] = useState<ReadonlySet<string>>(new Set());
  const [stopError, setStopError] = useState('');
  const { commands, runningAgents, finishedAgents } = tasks;

  const stop = async (taskId: string) => {
    setStopError('');
    setStopping((current) => new Set(current).add(taskId));
    try {
      await onStopCommand(taskId);
    } catch (cause) {
      setStopping((current) => new Set([...current].filter((id) => id !== taskId)));
      setStopError(cause instanceof Error ? cause.message : String(cause));
    }
  };
  const agentRow = (entry: SubagentEntry) => (
    <AgentRow
      key={entry.childRunId}
      entry={entry}
      selected={selectedChildRunId === entry.childRunId}
      onSelect={(id) => onSelect(selectedChildRunId === id ? null : id)}
    />
  );

  if (commands.length + runningAgents.length + finishedAgents.length === 0) {
    return (
      <div
        data-testid="session-tasks-panel"
        className="flex h-full items-center justify-center"
      >
        {projectionError ? (
          <ErrorNote className="m-3">
            {t('chat:subagent.load_failed')}: {projectionError}
          </ErrorNote>
        ) : (
          <EmptyState
            icon={<ListChecksIcon className="size-5" />}
            title={t('chat:tasks.empty_title')}
            hint={t('chat:tasks.empty_hint')}
          />
        )}
      </div>
    );
  }

  return (
    /* The panel's width discipline lives on the content box below, not on the
       scroller: the viewport hardcodes `overflow: scroll` on both axes, so
       there is no `overflow-x-hidden` left to cap a row that outgrows the
       panel — and only the vertical ScrollBar is rendered, so a horizontal
       overflow would scroll with nothing to grab. `w-full min-w-0 max-w-full`
       keeps the rows inside the panel's 30% instead, so that axis never
       engages. (Precedent: `SessionFilesPanel`.) */
    <ScrollArea data-testid="session-tasks-panel" className="h-full min-w-0">
      <div className="flex w-full min-w-0 max-w-full flex-col gap-4 p-2">
        {projectionError && (
          <ErrorNote>{t('chat:subagent.load_failed')}: {projectionError}</ErrorNote>
        )}
        {stopError && <ErrorNote>{stopError}</ErrorNote>}
        {(commands.length > 0 || runningAgents.length > 0) && (
          <TaskSection title={t('chat:tasks.running')} testId="session-tasks-running">
            {commands.map((task) => (
              <CommandRow
                key={task.task_id}
                task={task}
                stopping={stopping.has(task.task_id)}
                onStop={(taskId) => void stop(taskId)}
              />
            ))}
            {runningAgents.map(agentRow)}
          </TaskSection>
        )}
        {finishedAgents.length > 0 && (
          <TaskSection title={t('chat:tasks.finished')} testId="session-tasks-finished">
            {finishedAgents.map(agentRow)}
          </TaskSection>
        )}
      </div>
    </ScrollArea>
  );
}

export { shortId as shortSubagentId };
