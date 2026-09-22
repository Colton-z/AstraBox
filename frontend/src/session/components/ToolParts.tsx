import React, { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { diffLines } from 'diff';
import type { DynamicToolUIPart } from 'ai';
import { Tool, ToolContent, ToolInput, ToolOutput } from '@/components/ai-elements/tool';
import { CollapsibleTrigger } from '@/components/ui/collapsible';
import { Plan, PlanHeader, PlanTitle, PlanContent, PlanTrigger } from '@/components/ai-elements/plan';
import { Queue, QueueList, QueueItem, QueueItemIndicator, QueueItemContent } from '@/components/ai-elements/queue';
import { Task, TaskTrigger, TaskContent, TaskItem, TaskItemFile } from '@/components/ai-elements/task';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  ArrowRightIcon,
  CheckCircleIcon,
  ChevronDownIcon,
  CircleIcon,
  ClockIcon,
  WrenchIcon,
  XCircleIcon,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { useSubagentNavigation } from './subagentNavigation';
import {
  toolIcon,
  formatToolInputBody,
  localizeDisplayText,
  summarizeInlineValue,
} from '../../utils/format';
import { getToolStateLabelKey } from '../toolState';
import { formatOutput, parseTodoString } from '../utils/chatHelpers';

function getToolInput(dp: DynamicToolUIPart): Record<string, unknown> {
  return (typeof dp.input === 'object' && dp.input !== null ? dp.input : {}) as Record<string, unknown>;
}

function getToolApprovalApproved(dp: DynamicToolUIPart): boolean | null {
  const approval = (dp as { approval?: { approved?: boolean } }).approval;
  return typeof approval?.approved === 'boolean' ? approval.approved : null;
}

function getToolDisplayLabelKey(
  dp: DynamicToolUIPart,
  isPending: boolean,
  isActiveTurn: boolean,
): string {
  return getToolStateLabelKey({
    state: dp.state,
    providerExecuted: dp.providerExecuted === true,
    isPending,
    isActiveTurn,
    approvalApproved: getToolApprovalApproved(dp),
  });
}

function ToolStateBadge({ label }: { label: string }) {
  return (
    <Badge variant="secondary" className="shrink-0 text-xs">
      {label}
    </Badge>
  );
}

// What the state means, in the palette §7 assigns it: waiting is citrine,
// settled is mint, refused is plasma, failed is crimson. `approval-responded`
// is deliberately unpainted — it is a step on the way to one of the others,
// not a resting state a reader has to pick out.
const TOOL_STATE_ICON: Record<DynamicToolUIPart['state'], React.ReactNode> = {
  'approval-requested': <ClockIcon className="size-4 text-citrine-fg" />,
  'approval-responded': <CheckCircleIcon className="size-4" />,
  'input-available': <ClockIcon className="size-4 animate-pulse" />,
  'input-streaming': <CircleIcon className="size-4" />,
  'output-available': <CheckCircleIcon className="size-4 text-mint-fg" />,
  'output-denied': <XCircleIcon className="size-4 text-plasma-fg" />,
  'output-error': <XCircleIcon className="size-4 text-crimson-fg" />,
};

/**
 * The tool card's header row, with a status the vendored header cannot say.
 *
 * `ToolHeader` prints the raw part state in English. What belongs on the card
 * is the label `getToolStateLabelKey` derives — which reads `providerExecuted`,
 * whether the turn is still live, and how an approval was answered, so it
 * separates cases the state alone runs together — and it is translated.
 *
 * The row is composed from the same primitives the vendored header uses rather
 * than edited into it, which is how a product decision survives the next
 * re-vendor (docs/maintainers/upstream-drift-ledger.md). The chevron turns on
 * `data-open`, the attribute Base UI's Collapsible actually sets.
 */
export function ToolPartHeader({
  state,
  toolName,
  label,
}: {
  state: DynamicToolUIPart['state'];
  toolName: string;
  label: string;
}) {
  return (
    <CollapsibleTrigger
      aria-label={`${toolName} ${label}`}
      className="flex w-full items-center justify-between gap-4 p-3"
    >
      <div className="flex min-w-0 items-center gap-2">
        <WrenchIcon className="size-4 shrink-0 text-muted-foreground" />
        <span className="truncate font-medium text-sm">{toolName}</span>
        <span className="flex shrink-0 items-center gap-1.5">
          {TOOL_STATE_ICON[state]}
          <ToolStateBadge label={label} />
        </span>
      </div>
      <ChevronDownIcon className="size-4 shrink-0 text-muted-foreground transition-transform group-data-open:rotate-180" />
    </CollapsibleTrigger>
  );
}

export function ToolPartCard({
  dp,
  isPending,
  isActiveTurn,
}: {
  dp: DynamicToolUIPart;
  isPending: boolean;
  isActiveTurn: boolean;
}) {
  if (dp.toolName === 'Edit' || dp.toolName === 'Write') {
    return <FileChangePartCard dp={dp} isPending={isPending} isActiveTurn={isActiveTurn} />;
  }
  if (dp.toolName === 'TodoWrite') {
    return <TodoWritePartCard dp={dp} isPending={isPending} isActiveTurn={isActiveTurn} />;
  }
  if (dp.toolName === 'AskUserQuestion') {
    return <AskUserQuestionPartCard dp={dp} isPending={isPending} isActiveTurn={isActiveTurn} />;
  }
  if (dp.toolName === 'ExitPlanMode') {
    return <ExitPlanModePartCard dp={dp} isPending={isPending} isActiveTurn={isActiveTurn} />;
  }
  return <GenericToolPartCard dp={dp} isPending={isPending} isActiveTurn={isActiveTurn} />;
}

function GenericToolPartCard({
  dp,
  isPending,
  isActiveTurn,
}: {
  dp: DynamicToolUIPart;
  isPending: boolean;
  isActiveTurn: boolean;
}) {
  const { t } = useTranslation();
  const label = t(getToolDisplayLabelKey(dp, isPending, isActiveTurn));
  const navigation = useSubagentNavigation();
  const childRuns = navigation.agents.filter((agent) => agent.toolCallIds.includes(dp.toolCallId));
  const toolBorder = isPending
    ? 'border-citrine/45'
    : dp.state === 'output-error' || dp.state === 'output-denied'
      ? 'border-destructive/40'
      : undefined;

  // `mb-0`: the kit's Tool carries `mb-4`, which would stack onto the `gap-2.5`
  // MessageContent lays between parts and leave a trailing gap under a message
  // that ends in a tool card. Spacing here is the parent's gap alone
  // (`docs/frontend-design.md` §0).
  return (
    <Tool data-tool-call-id={dp.toolCallId} className={cn('mb-0 bg-card/40 transition-colors', toolBorder)}>
      <ToolPartHeader state={dp.state} toolName={dp.toolName} label={label} />
      <ToolContent>
        <ToolInput input={dp.input} />
        {(dp.state === 'output-available' || dp.state === 'output-error') && (
          <ToolOutput output={dp.output} errorText={dp.errorText} />
        )}
        {childRuns.map((child) => (
          <div key={child.childRunId} className="flex items-center justify-between gap-2 border-t border-border bg-muted/30 px-3 py-1.5">
            <span className="font-mono text-10 text-muted-foreground">
              subagent <span className="rounded bg-muted px-1">{child.childRunId.slice(0, 8)}</span>
            </span>
            <Button
              size="sm"
              variant="ghost"
              className="h-7 gap-1 text-xs"
              onClick={() => navigation.openSubagent(child.childRunId)}
            >
              {t('chat:tool.open_in_agents')}
              <ArrowRightIcon className="size-3" />
            </Button>
          </div>
        ))}
      </ToolContent>
    </Tool>
  );
}

function FileChangePartCard({
  dp,
  isPending,
  isActiveTurn,
}: {
  dp: DynamicToolUIPart;
  isPending: boolean;
  isActiveTurn: boolean;
}) {
  const { t } = useTranslation();
  const input = getToolInput(dp);
  const isEdit = dp.toolName === 'Edit';
  const filePath = String(input.file_path ?? input.path ?? 'unknown');
  const shortPath = filePath.split('/').slice(-3).join('/');
  const isDenied = dp.state === 'output-denied' || getToolApprovalApproved(dp) === false;
  const hasError = dp.state === 'output-error';
  const label = t(getToolDisplayLabelKey(dp, isPending, isActiveTurn));

  const oldStr = isEdit ? String(input.old_string ?? '') : '';
  const newStr = isEdit ? String(input.new_string ?? '') : String(input.content ?? '');

  const diffResult = useMemo(() => {
    if (isEdit && oldStr && newStr) return diffLines(oldStr, newStr);
    if (!isEdit && newStr) return diffLines('', newStr);
    return [];
  }, [isEdit, oldStr, newStr]);

  const linesAdded = diffResult.filter(p => p.added).reduce((s, p) => s + (p.count ?? 0), 0);
  const linesRemoved = diffResult.filter(p => p.removed).reduce((s, p) => s + (p.count ?? 0), 0);

  return (
    <Task data-tool-call-id={dp.toolCallId} defaultOpen={isPending}>
      <TaskTrigger title={shortPath}>
        <div className="flex w-full cursor-pointer items-center gap-2 text-sm">
          <span className="shrink-0">
            {isPending ? '\u23F3' : hasError || isDenied ? <span className="text-destructive">{'\u2717'}</span> : dp.state === 'output-available' ? <span className="text-mint-fg">{'\u2713'}</span> : '\u25CB'}
          </span>
          <TaskItemFile>{shortPath}</TaskItemFile>
          {/* Gains and losses take the palette's completed and failed hues;
              "New" takes none, because it is a fact about the file rather than
              a run state, and §7 keeps the status hues for run states. */}
          <span className="flex items-center gap-1 text-xs">
            {linesAdded > 0 && <span className="text-mint-fg">+{linesAdded}</span>}
            {linesRemoved > 0 && <span className="text-crimson-fg">-{linesRemoved}</span>}
            {!isEdit && <span className="rounded-full border bg-muted px-1.5 text-10 text-muted-foreground">{t('chat:tool.new_file')}</span>}
          </span>
          <ToolStateBadge label={label} />
          {/* `group-data-panel-open`, not `group-data-[state=open]`: the group
              is the Base UI collapsible TRIGGER, which marks itself with
              `data-panel-open` — `data-open`/`data-closed` are the root's. The
              Radix spelling matched nothing, so the arrow never turned. */}
          <ChevronDownIcon className="ml-auto size-4 shrink-0 text-muted-foreground transition-transform group-data-panel-open:rotate-180" />
        </div>
      </TaskTrigger>
      <TaskContent>
        {diffResult.length > 0 && (
          <div
            data-slot="verbatim"
            className="overflow-x-auto rounded-md border border-border bg-muted/30 font-mono text-xs leading-5"
          >
            {diffResult.map((part, i) => {
              const lines = part.value.replace(/\n$/, '').split('\n');
              return lines.map((line, j) => (
                <div
                  key={`${i}-${j}`}
                  className={`flex ${part.added ? 'bg-mint/10 text-mint-fg' : part.removed ? 'bg-crimson/10 text-crimson-fg' : 'text-muted-foreground'}`}
                >
                  <span className="select-none w-5 shrink-0 text-center opacity-50">{part.added ? '+' : part.removed ? '-' : ' '}</span>
                  <span className="flex-1 whitespace-pre-wrap break-all px-1">{line}</span>
                </div>
              ));
            })}
          </div>
        )}
        {dp.state === 'output-available' && dp.output !== undefined && (
          <TaskItem>
            <pre className="whitespace-pre-wrap break-words text-xs text-muted-foreground bg-muted/50 rounded-md p-2 mt-1">{localizeDisplayText(formatOutput(dp.output))}</pre>
          </TaskItem>
        )}
        {dp.state === 'output-error' && dp.errorText && (
          <TaskItem>
            <pre className="whitespace-pre-wrap break-words text-xs text-destructive bg-destructive/10 rounded-md p-2 mt-1">{localizeDisplayText(String(dp.errorText))}</pre>
          </TaskItem>
        )}
      </TaskContent>
    </Task>
  );
}

function TodoWritePartCard({
  dp,
}: {
  dp: DynamicToolUIPart;
  isPending: boolean;
  isActiveTurn: boolean;
}) {
  const { t } = useTranslation();
  const input = getToolInput(dp);
  const todos = parseTodoString(input.todos);
  if (todos.length === 0) return null;

  return (
    <Queue data-tool-call-id={dp.toolCallId}>
      {/* QueueList is the `<ul>` the QueueItem `<li>` rows require; without it
          the list items had no list to belong to. The inner row div stays:
          QueueItem is flex-col, and these three cells sit on one line. */}
      {/* `my-0` for the reason ComposerQueue spells out: QueueList's `mt-2`
          is the gap under a QueueSection header this card does not have. */}
      <QueueList className="my-0">
        {todos.map((todo, index) => {
          const status = String(todo.status ?? 'pending');
          const content = summarizeInlineValue(todo.content) || t('chat:tool.todo_item', { index: index + 1 });
          const completed = status === 'completed';
          return (
            <QueueItem key={`${content}:${index}`}>
              <div className="flex items-center gap-2">
                <QueueItemIndicator completed={completed} />
                <QueueItemContent completed={completed}>{content}</QueueItemContent>
                {status === 'in_progress' && (
                  <span className="text-xs text-citrine-fg shrink-0">{t('chat:tool.in_progress')}</span>
                )}
              </div>
            </QueueItem>
          );
        })}
      </QueueList>
    </Queue>
  );
}

function AskUserQuestionPartCard({
  dp,
  isPending,
  isActiveTurn,
}: {
  dp: DynamicToolUIPart;
  isPending: boolean;
  isActiveTurn: boolean;
}) {
  const { t } = useTranslation();
  const input = getToolInput(dp);
  const questions = Array.isArray(input.questions) ? (input.questions as Array<Record<string, unknown>>) : [];
  const label = t(getToolDisplayLabelKey(dp, isPending, isActiveTurn));

  return (
    <Card data-tool-call-id={dp.toolCallId} size="sm" className={isPending ? 'border-citrine/50' : undefined}>
      <CardHeader className="flex flex-row items-center justify-between space-y-0">
        <CardTitle className="flex items-center gap-2">
          <span>{toolIcon('AskUserQuestion')}</span>
          <span>{t('chat:tool.user_questionnaire')}</span>
        </CardTitle>
        <ToolStateBadge label={label} />
      </CardHeader>
      <CardContent className="space-y-3">
        {questions.length > 0 ? (
          questions.map((question, index) => {
            const options = Array.isArray(question.options) ? (question.options as Array<Record<string, unknown>>) : [];
            const multiSelect = question.multiSelect === true || question.multi_select === true;
            return (
              <div key={`q-${index}`} className="space-y-1.5">
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <span>{t('chat:tool.question_n', { index: index + 1 })}</span>
                  <span className="rounded-full bg-muted px-1.5 py-0.5">{multiSelect ? t('chat:tool.multi_select') : t('chat:tool.single_select')}</span>
                </div>
                {/* Header, prompt, and options are the engine's
                    AskUserQuestion strings quoted into the transcript — the
                    verbatim slot marks their casing and wording as the
                    model's, not the console's. */}
                {summarizeInlineValue(question.header) && <h4 data-slot="verbatim" className="text-sm font-medium">{summarizeInlineValue(question.header)}</h4>}
                {summarizeInlineValue(question.question) && <p data-slot="verbatim" className="text-sm text-muted-foreground">{summarizeInlineValue(question.question)}</p>}
                {options.length > 0 && (
                  <div data-slot="verbatim" className="flex flex-wrap gap-1.5 mt-1">
                    {options.map((option, oi) => (
                      <span key={`o-${oi}`} className="inline-flex flex-col rounded-md border border-border bg-muted/50 px-2 py-1 text-xs">
                        <strong className="text-foreground">{summarizeInlineValue(option.label) || t('chat:tool.option_n', { index: oi + 1 })}</strong>
                        {summarizeInlineValue(option.description) && <small className="text-xs text-muted-foreground">{summarizeInlineValue(option.description)}</small>}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            );
          })
        ) : (
          <pre className="whitespace-pre-wrap break-words text-xs font-mono text-muted-foreground bg-muted/50 rounded-md p-2">{formatToolInputBody('AskUserQuestion', input)}</pre>
        )}
        {dp.state === 'output-available' && dp.output !== undefined && (
          <div className="border-t border-border pt-2 mt-2">
            <div className="text-xs font-medium text-muted-foreground mb-1">{t('chat:tool.output')}</div>
            <pre className="whitespace-pre-wrap break-words text-xs font-mono text-muted-foreground bg-muted/50 rounded-md p-2">{localizeDisplayText(formatOutput(dp.output))}</pre>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ExitPlanModePartCard({
  dp,
  isPending,
  isActiveTurn,
}: {
  dp: DynamicToolUIPart;
  isPending: boolean;
  isActiveTurn: boolean;
}) {
  const { t } = useTranslation();
  const input = getToolInput(dp);
  const plan = String(input.plan ?? '').trim();
  const title = String(input.title ?? 'Plan').trim();
  const label = t(getToolDisplayLabelKey(dp, isPending, isActiveTurn));

  return (
    <Plan data-tool-call-id={dp.toolCallId} isStreaming={isPending} defaultOpen={isPending}>
      <PlanHeader>
        <div className="flex min-w-0 items-center gap-2">
          <PlanTitle>{title || t('chat:tool.execution_plan')}</PlanTitle>
          <ToolStateBadge label={label} />
        </div>
        {/* The vendored trigger names itself "Toggle plan" in English; an
            aria-label on the button is what a reader's screen reader takes
            instead, so the name arrives in their language. */}
        <PlanTrigger aria-label={t('misc:plan.toggle')} />
      </PlanHeader>
      <PlanContent>
        {plan && (
          <div className="whitespace-pre-wrap break-words rounded-md bg-muted/50 p-3 text-sm text-muted-foreground">
            {plan}
          </div>
        )}
        {dp.state === 'output-available' && dp.output !== undefined && (
          <div className="mt-2 text-xs text-muted-foreground">
            {localizeDisplayText(formatOutput(dp.output))}
          </div>
        )}
      </PlanContent>
    </Plan>
  );
}
