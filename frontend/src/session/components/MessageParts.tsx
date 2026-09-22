import React, { useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { UIMessage as SDKUIMessage, DynamicToolUIPart } from 'ai';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { ChevronDownIcon, ListTreeIcon, LoaderCircleIcon } from 'lucide-react';
import { Shimmer } from '@/components/ai-elements/shimmer';
/*
  Message and MessageContent only. `MessageResponse` renders Streamdown, and
  one markdown renderer per transcript is the rule `ThinkingPartCard` below
  states in full: everything here goes through `MarkdownContent`, the only path
  that applies `localizeDisplayText` to vendor text.
*/
import { Image } from '@/components/ai-elements/image';
import { Message, MessageContent } from '@/components/ai-elements/message';
import { Reasoning, ReasoningTrigger } from '@/components/ai-elements/reasoning';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { cn } from '@/lib/utils';
import { useTranscriptAccess } from '../TranscriptAccess';
import type { ProcessDetails, ProcessSummaryState } from '../../types';
import { localizeDisplayText, toolPreview } from '../../utils/format';
import { filterSupersededSingletonInteractionParts } from '../toolState';
import { getSdkMessageTurnId } from '../utils/chatHelpers';
import { LazyProcessBlock, ProcessSummaryCache } from './LazyProcessBlock';
import { ToolPartCard } from './ToolParts';
import { ApiRetryNote, ResultPartCard, TurnFailureCard } from './ResultCards';
import type { ApiRetryData } from './ResultCards';

type MessagePart = SDKUIMessage['parts'][number];

/** Consecutive parts a single disclosure stands for, in source order. */
interface PartGroup {
  key: number;
  process: boolean;
  parts: MessagePart[];
}

// What a reader acts on or reads as the turn's outcome. These never join a
// process group, and when a whole turn folds they stay outside its header.
const STANDALONE_DATA_PARTS = new Set([
  'data-result',
  'data-turn-failure',
  'data-api-retry',
  'data-process-block',
  'data-interaction',
]);
// The outcome and the decisions inside a turn. A group holding one of these is
// never folded behind the turn's own header, whichever side of the last tool
// call it sits on. A folded header is not one of them: it is already a fold.
const ALERT_DATA_PARTS = new Set([
  'data-result',
  'data-turn-failure',
  'data-api-retry',
  'data-interaction',
]);
const FAILED_TOOL_STATES = new Set(['output-error', 'output-denied']);
const UNFINISHED_TOOL_STATES = new Set([
  'input-streaming',
  'input-available',
  'approval-requested',
  'approval-responded',
]);
// The verb for the tools a reader watches most. Anything else falls back to
// `running_tool`, which names the tool rather than guessing at what it does.
const PROCESS_ACTION_KEYS: Record<string, string> = {
  Read: 'chat:process.action.read',
  Write: 'chat:process.action.write',
  Edit: 'chat:process.action.edit',
  Bash: 'chat:process.action.bash',
  Glob: 'chat:process.action.glob',
  Grep: 'chat:process.action.grep',
};
const TURN_COLLAPSE_SETTLE_MS = 1000;
const PROCESS_SUMMARY_POLL_MS = 1000;

function toolApprovalApproved(part: DynamicToolUIPart): boolean | null {
  const approval = (part as { approval?: { approved?: boolean } }).approval;
  return typeof approval?.approved === 'boolean' ? approval.approved : null;
}

/**
 * Is this part work the reader can look through afterwards, rather than read?
 *
 * Thinking and a tool call that ran to an answer are process. A tool waiting on
 * this reader, or one that ended denied or in error, is not: those are the
 * places a turn needs a decision or an explanation, and folding them away puts
 * the one thing the reader has to see behind a disclosure.
 */
function isProcessPart(
  part: MessagePart,
  pendingToolCallId?: string,
  settled = false,
): boolean {
  if (part.type === 'reasoning') return true;
  if (part.type !== 'dynamic-tool') return false;
  const dp = part as DynamicToolUIPart;
  // Once the turn has ended, a call that never returned is not work that ran:
  // it is the call the stop landed on, and the durable projection leaves it
  // outside the fold for the same reason it leaves an errored one there.
  if (settled && UNFINISHED_TOOL_STATES.has(dp.state)) return false;
  return dp.toolCallId !== pendingToolCallId
    && dp.state !== 'approval-requested'
    && !FAILED_TOOL_STATES.has(dp.state)
    && !(dp.state === 'approval-responded' && toolApprovalApproved(dp) === false);
}

/**
 * Split one assistant message's visible parts into disclosure groups.
 *
 * A step boundary and the data parts the console carries alongside a turn join
 * whichever group is open: neither is something a reader looks at on its own,
 * and letting one start a group would cut a run of work in half.
 */
export function groupAssistantProcess(
  parts: MessagePart[],
  pendingToolCallId?: string,
  settled = false,
): PartGroup[] {
  const groups: PartGroup[] = [];
  parts.forEach((part, index) => {
    if (
      part.type === 'step-start'
      || (part.type.startsWith('data-') && !STANDALONE_DATA_PARTS.has(part.type))
    ) {
      groups[groups.length - 1]?.parts.push(part);
      return;
    }
    // Blank text renders nothing, so ending a process run on one would break
    // the run at a part the reader never sees.
    if (part.type === 'text' && !part.text.trim()) return;
    const process = isProcessPart(part, pendingToolCallId, settled);
    const previous = groups[groups.length - 1];
    if (process && previous?.process) previous.parts.push(part);
    else groups.push({ key: index, process, parts: [part] });
  });
  return groups;
}

interface PartGroupRenderContext {
  groupOwnerId: string;
  pendingToolCallId?: string;
  isActiveTurn: boolean;
  isStreaming?: boolean;
  lastVisiblePart?: MessagePart;
  sessionId?: string;
  /**
   * Which groups the reader has opened, by group key. Held by the owner of
   * the parts rather than by each group: the turn's fold re-parents every
   * group when it lands, which remounts them, and a group that kept its own
   * state would come back closed under a reader who had just opened it.
   */
  openGroups: Record<number, boolean>;
  setGroupOpen: (key: number, open: boolean) => void;
  /** User text carries no `assistant-text` hook; the renderer keys it off this. */
  isUser?: boolean;
}

function PartGroupRenderer({
  group,
  running,
  context,
}: {
  group: PartGroup;
  running: boolean;
  context: PartGroupRenderContext;
}) {
  const rendered = group.parts.map((part, index) => (
    <PartRenderer
      key={`${context.groupOwnerId}-${group.key}-part-${index}`}
      part={part}
      pendingToolCallId={context.pendingToolCallId}
      isActiveTurn={context.isActiveTurn}
      isStreaming={context.isStreaming && part === context.lastVisiblePart}
      isUser={context.isUser}
      sessionId={context.sessionId}
    />
  ));
  if (!group.process) return <>{rendered}</>;
  return (
    <AssistantProcess
      parts={group.parts}
      running={running}
      open={context.openGroups[group.key] ?? false}
      onOpenChange={(open) => context.setGroupOpen(group.key, open)}
    >
      {rendered}
    </AssistantProcess>
  );
}

/** Reader-owned open state for a message's process groups, keyed by group. */
function useOpenGroups(): [Record<number, boolean>, (key: number, open: boolean) => void] {
  const [openGroups, setOpenGroups] = useState<Record<number, boolean>>({});
  const setGroupOpen = useCallback((key: number, open: boolean) => {
    setOpenGroups((current) => (current[key] === open ? current : { ...current, [key]: open }));
  }, []);
  return [openGroups, setGroupOpen];
}

/**
 * One assistant message's parts, grouped and rendered.
 *
 * The bubble below renders through this, and so does a folded header once its
 * blocks are fetched — the work a reader opens has to look like the work they
 * did not fold, or the same tool call reads as two different things depending
 * on which side of a disclosure it is on. It renders parts only: the message
 * chrome belongs to whoever owns the row.
 */
export function AssistantMessageParts({
  parts,
  partOwnerId,
  pendingToolCallId,
  isActiveTurn = false,
  isStreaming,
  sessionId,
}: {
  parts: MessagePart[];
  partOwnerId: string;
  pendingToolCallId?: string;
  isActiveTurn?: boolean;
  isStreaming?: boolean;
  sessionId?: string;
}) {
  const groups = useMemo(
    () => groupAssistantProcess(parts, pendingToolCallId),
    [parts, pendingToolCallId],
  );
  const [openGroups, setGroupOpen] = useOpenGroups();
  return (
    <>
      {groups.map((group, index) => (
        <PartGroupRenderer
          key={`${partOwnerId}-group-${group.key}`}
          group={group}
          running={Boolean(isStreaming && index === groups.length - 1 && !pendingToolCallId)}
          context={{
            groupOwnerId: partOwnerId,
            pendingToolCallId,
            isActiveTurn,
            isStreaming,
            lastVisiblePart: parts[parts.length - 1],
            sessionId,
            openGroups,
            setGroupOpen,
          }}
        />
      ))}
    </>
  );
}

export function MessageBubble({
  message,
  pendingToolCallId,
  activeTurnId,
  isStreaming,
  sessionId,
}: {
  message: SDKUIMessage;
  pendingToolCallId?: string;
  activeTurnId?: string | null;
  isStreaming?: boolean;
  sessionId?: string;
}) {
  const { t } = useTranslation();
  const isActiveTurn = message.role === 'assistant' && getSdkMessageTurnId(message) === String(activeTurnId ?? '').trim();
  const visibleParts = filterSupersededSingletonInteractionParts(message.parts, pendingToolCallId);
  const partOwnerId = message.role === 'assistant'
    ? getSdkMessageTurnId(message) || message.id
    : message.id;

  const isUser = message.role === 'user';
  const settled = !isUser && !isStreaming && !isActiveTurn;
  const groups = useMemo(
    () => (isUser
      ? [{ key: 0, process: false, parts: visibleParts }]
      : groupAssistantProcess(visibleParts, pendingToolCallId, settled)),
    [isUser, visibleParts, pendingToolCallId, settled],
  );

  // How the turn ended, from the platform's own settled facts. A `result` that
  // reports an error and a `turn_failure` block are the two ways a turn stops
  // short; an idle turn carrying neither of them ended normally.
  const result = visibleParts.filter((part) => part.type === 'data-result').at(-1) as
    { data: { is_error?: boolean } } | undefined;
  const interruptedEnd = visibleParts.some((part) => part.type === 'data-turn-failure')
    || result?.data.is_error === true;
  const normalEnd = !interruptedEnd && !isStreaming && !isActiveTurn;

  const lastToolGroup = groups.findLastIndex(
    (group) => group.parts.some((part) => part.type === 'dynamic-tool'),
  );
  const hasConclusion = groups.slice(lastToolGroup + 1).some(
    (group) => group.parts.some((part) => part.type === 'text' && part.text.trim()),
  );
  // A tool that failed is folded away only when the answer below it accounts
  // for it: the model went on and said something afterwards. On an interrupted
  // turn nothing came after, so the failure is the last thing that happened and
  // stays where the reader can see it. A call that never returned — the one a
  // stop landed on, or one still waiting on an answer — stays outside on every
  // kind of end: the fold only ever applies to a settled turn, and the durable
  // projection leaves a call without a result outside for the same reason.
  const isAlertPart = (part: MessagePart) => ALERT_DATA_PARTS.has(part.type)
    || (
      part.type === 'dynamic-tool'
      && (
        UNFINISHED_TOOL_STATES.has((part as DynamicToolUIPart).state)
        || (
          !(normalEnd && hasConclusion)
          && (
            FAILED_TOOL_STATES.has((part as DynamicToolUIPart).state)
            || ((part as DynamicToolUIPart).state === 'approval-responded'
              && toolApprovalApproved(part as DynamicToolUIPart) === false)
          )
        )
      )
    );
  const hasPendingPart = visibleParts.some((part) => part.type === 'dynamic-tool'
    && ((part as DynamicToolUIPart).toolCallId === pendingToolCallId
      || (part as DynamicToolUIPart).state === 'approval-requested'));
  const processGroups = groups.filter(
    (group, index) => index <= lastToolGroup && !group.parts.some(isAlertPart),
  );
  const outsideGroups = groups.filter(
    (group, index) => index > lastToolGroup || group.parts.some(isAlertPart),
  );
  const canCollapseTurn = message.role === 'assistant'
    && !isStreaming
    && !hasPendingPart
    && lastToolGroup >= 0
    && processGroups.length > 0
    && ((normalEnd && hasConclusion) || interruptedEnd);
  const hasTools = visibleParts.some((part) => part.type === 'dynamic-tool');

  // A turn settles one part at a time, so the shape that decides the fold is
  // final only a moment after the last part lands. Waiting stops the answer
  // from being rearranged under a reader who is already reading it.
  const [collapseReady, setCollapseReady] = useState(canCollapseTurn);
  useEffect(() => {
    if (!canCollapseTurn) {
      setCollapseReady(false);
      return;
    }
    const timer = window.setTimeout(() => setCollapseReady(true), TURN_COLLAPSE_SETTLE_MS);
    return () => window.clearTimeout(timer);
  }, [canCollapseTurn]);

  // The label, remembered per response beyond this row's own life: the list
  // virtualizes, so a settled turn scrolled out of view and back is a fresh
  // component, and asking again would repaint a header that already has its
  // label as "summarising" until the same answer came back.
  const summaryCache = useContext(ProcessSummaryCache);
  const { generateSummary } = useTranscriptAccess();
  const [summary, setSummary] = useState<ProcessSummaryState | null>(
    () => summaryCache?.get(message.id) ?? null,
  );
  useEffect(() => {
    if (!canCollapseTurn || !sessionId || !hasTools || !generateSummary) return;
    const known = summaryCache?.get(message.id);
    if (known && known.status !== 'generating') {
      setSummary(known);
      return;
    }
    let cancelled = false;
    let timer: number | undefined;
    const ask = async () => {
      try {
        const state = await generateSummary(sessionId, message.id);
        if (cancelled) return;
        setSummary(state);
        summaryCache?.set(message.id, state);
        if (state.status === 'generating') {
          timer = window.setTimeout(() => { void ask(); }, PROCESS_SUMMARY_POLL_MS);
        }
      } catch {
        // A label the deployment could not write is not a failure of the turn,
        // and the work behind the header is intact either way. The header keeps
        // its generic title rather than carrying an error into the transcript.
        if (!cancelled) setSummary({ status: 'failed' });
      }
    };
    setSummary({ status: 'generating' });
    void ask();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [canCollapseTurn, sessionId, hasTools, message.id, summaryCache, generateSummary]);

  const [openGroups, setGroupOpen] = useOpenGroups();
  const renderGroup = (group: PartGroup, index: number) => (
    <PartGroupRenderer
      key={`${partOwnerId}-group-${group.key}`}
      group={group}
      running={Boolean(isStreaming && index === groups.length - 1 && !pendingToolCallId)}
      context={{
        groupOwnerId: partOwnerId,
        pendingToolCallId,
        isActiveTurn,
        isStreaming,
        lastVisiblePart: visibleParts[visibleParts.length - 1],
        sessionId,
        openGroups,
        setGroupOpen,
        isUser,
      }}
    />
  );

  return (
    /*
      `astra-enter` is this transcript's block-enter motion, which the kit has
      no opinion about. `max-w-full` undoes the kit's `max-w-[95%]`: that cap
      stopped a tool row 37px short of where the user bubble above it ended —
      two columns a reader takes as one, never lining up, at a width that is on
      no scale.
    */
    <Message
      from={message.role}
      data-testid={isUser ? 'user-message' : 'assistant-message'}
      data-streaming={!isUser && isStreaming ? 'true' : undefined}
      className="astra-enter max-w-full"
    >
      {/*
        Two kinds of box share this element, and they want opposite widths. A
        user message is a bubble: it hugs its text, so `w-fit`. An assistant
        turn is a stack of structural blocks — tool cards, diffs, a usage row —
        which should fill the column. The kit ships `w-fit` for both, so the
        assistant case is the override.

        On the assistant side, `w-fit` would size the whole stack to its longest
        sentence because `w-full` children contribute nothing to `fit-content`.
        Tool cards instead follow the layout width and stay stable while text
        streams.

        `overflow-clip` is the other override: the kit's
        `overflow-hidden` sits in Tailwind's utilities layer, which wins over
        `.clip-content` in the components layer. Clipping preserves the margin
        needed by the live indicator's halo without creating a scroll container.
      */}
      <MessageContent className="clip-content w-full gap-2.5 overflow-clip group-[.is-user]:w-fit group-[.is-user]:max-w-[85%] group-[.is-user]:rounded-2xl group-[.is-user]:rounded-tr-md group-[.is-user]:border group-[.is-user]:border-border group-[.is-user]:px-3.5 group-[.is-user]:py-2.5">
        {canCollapseTurn && collapseReady ? (
          <>
            <AssistantTurnProcess summary={summary}>
              {processGroups.map(renderGroup)}
            </AssistantTurnProcess>
            {outsideGroups.map(renderGroup)}
            {interruptedEnd && (
              <div role="status" className="text-sm text-muted-foreground">
                {t('chat:process.stopped')}
              </div>
            )}
          </>
        ) : groups.map(renderGroup)}
        {/*
          The fallback pulse, for a surface that says nothing itself: no parts
          yet, or a group of plain text. A process group runs its own spinner
          and a pending tool card its own waiting state, and either beside this
          line would give one state two names.
        */}
        {isStreaming && !pendingToolCallId && !groups.at(-1)?.process && (
          <div className="flex items-center gap-2">
            <span className="astra-dot astra-dot--live" />
            <Shimmer duration={1.5}>{t('chat:message.generating')}</Shimmer>
          </div>
        )}
      </MessageContent>
    </Message>
  );
}

/** The title a folded process carries, whatever stage its label is at. */
function useProcessTitle(summary: ProcessSummaryState | null | undefined): string {
  const { t } = useTranslation();
  const text = String(summary?.summary ?? '').trim();
  if (text) return text;
  if (summary?.status === 'generating') return t('chat:process.summary_generating');
  return t('chat:process.title');
}

/**
 * The whole of one settled response's work, behind one header.
 *
 * The panel stays mounted while it is closed: the tool cards inside hold their
 * own open state, and remounting them would close every one a reader had
 * already opened whenever the header was shut.
 */
function AssistantTurnProcess({
  summary,
  children,
}: {
  summary: ProcessSummaryState | null;
  children: React.ReactNode;
}) {
  const title = useProcessTitle(summary);
  return (
    <Collapsible
      className="group w-full min-w-0 overflow-hidden rounded-[var(--radius)] border border-border bg-muted/30"
      data-testid="assistant-turn-process"
    >
      <CollapsibleTrigger
        data-testid="assistant-turn-process-trigger"
        className="ring-inward flex w-full items-center gap-2.5 p-3 text-left text-sm hover:bg-accent/40"
      >
        <span className="flex size-7 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground">
          <ListTreeIcon aria-hidden="true" className="size-4" />
        </span>
        <span className="min-w-0 flex-1 truncate font-medium text-muted-foreground">{title}</span>
        <ChevronDownIcon
          aria-hidden="true"
          className="size-4 shrink-0 text-muted-foreground transition-transform group-data-open:rotate-180"
        />
      </CollapsibleTrigger>
      <CollapsibleContent
        keepMounted
        data-testid="assistant-turn-process-panel"
        className="flex min-w-0 flex-col gap-2.5 border-t border-border px-3 py-3"
      >
        {children}
      </CollapsibleContent>
    </Collapsible>
  );
}

/**
 * One run of thinking and finished tool calls, behind one header.
 *
 * While the run is live the header is the only thing on screen saying what the
 * model is doing, so it carries a status line naming the tool in flight. That
 * line is derived from the latest part's TYPE, never from a reasoning part's
 * done state: the translator closes each thinking fragment on its own, so a
 * model that thinks in bursts would otherwise flicker the line on and off
 * between fragments of one continuous thought.
 */
function AssistantProcess({
  parts,
  running,
  open,
  onOpenChange,
  children,
}: {
  parts: MessagePart[];
  running: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: React.ReactNode;
}) {
  const { t } = useTranslation();
  const tools = parts.filter((part) => part.type === 'dynamic-tool') as DynamicToolUIPart[];
  const completed = tools.filter((part) => part.state === 'output-available').length;
  const unfinished = tools.filter((part) => UNFINISHED_TOOL_STATES.has(part.state));
  const current = unfinished[unfinished.length - 1];
  const input = current && typeof current.input === 'object' && current.input !== null
    ? current.input as Record<string, unknown>
    : {};
  const preview = current ? toolPreview(current.toolName, input) : '';
  const latestActivity = parts.findLast(
    (part) => part.type === 'reasoning' || part.type === 'dynamic-tool' || part.type === 'text',
  );
  const verb = current
    ? (current.state === 'input-streaming'
      ? t('chat:process.preparing', { tool: current.toolName })
      : t(PROCESS_ACTION_KEYS[current.toolName] ?? 'chat:process.running_tool', { tool: current.toolName }))
    : '';
  const action = current
    ? `${verb}${preview ? ` · ${preview}` : ''}`
    : latestActivity?.type === 'reasoning' ? t('chat:process.thinking') : '';
  const count = running
    ? t('chat:process.completed_count', { count: completed })
    : tools.length
      ? t('chat:process.tool_calls', { count: tools.length })
      : t('chat:process.reasoning_only');

  return (
    <Collapsible
      className="group w-full min-w-0 overflow-hidden rounded-[var(--radius)] border border-border bg-muted/30"
      data-testid="assistant-process"
      open={open}
      onOpenChange={onOpenChange}
    >
      <CollapsibleTrigger
        data-testid="assistant-process-trigger"
        className="ring-inward flex w-full items-center gap-2.5 p-3 text-left text-sm hover:bg-accent/40"
      >
        <span
          className={cn(
            'flex size-7 shrink-0 items-center justify-center rounded-md',
            running ? 'bg-teal-tint text-teal-fg' : 'bg-muted text-muted-foreground',
          )}
        >
          {running ? (
            <LoaderCircleIcon aria-hidden="true" className="size-4 animate-spin motion-reduce:animate-none" />
          ) : (
            <ListTreeIcon aria-hidden="true" className="size-4" />
          )}
        </span>
        <span className={cn('font-medium', running ? 'text-foreground' : 'text-muted-foreground')}>
          {running
            ? t('chat:process.working')
            : t(tools.length ? 'chat:process.tools_title' : 'chat:process.reasoning_title')}
        </span>
        <span className="ml-auto shrink-0 text-xs text-muted-foreground">{count}</span>
        <ChevronDownIcon
          aria-hidden="true"
          className="size-4 shrink-0 text-muted-foreground transition-transform group-data-open:rotate-180"
        />
      </CollapsibleTrigger>
      {running && (
        <div
          role="status"
          data-testid="assistant-process-activity"
          className="ml-6 min-h-5 border-l border-border pb-3 pl-6 text-sm leading-5 text-muted-foreground"
        >
          {action && <div className="truncate" title={action}>{action}</div>}
          {unfinished.length > 1 && (
            <div className="mt-0.5 text-xs">
              {t('chat:process.more_in_progress', { count: unfinished.length - 1 })}
            </div>
          )}
        </div>
      )}
      <CollapsibleContent
        keepMounted
        data-testid="assistant-process-panel"
        className="flex min-w-0 flex-col gap-2.5 border-t border-border px-3 py-3"
      >
        {children}
      </CollapsibleContent>
    </Collapsible>
  );
}

export function PartRenderer({
  part,
  pendingToolCallId,
  isActiveTurn,
  isStreaming,
  isUser,
  sessionId,
}: {
  part: SDKUIMessage['parts'][number];
  pendingToolCallId?: string;
  isActiveTurn: boolean;
  isStreaming?: boolean;
  isUser?: boolean;
  sessionId?: string;
}) {
  switch (part.type) {
    case 'text':
      if (!part.text) return null;
      // Assistant text carries a stable hook for e2e; user text and reasoning do not.
      return <MarkdownContent text={part.text} testid={isUser ? undefined : 'assistant-text'} />;
    case 'reasoning':
      // Native thinking blocks can contain no visible text. Hide settled
      // empty cards, but retain the activity indicator while streaming.
      if (!part.text?.trim() && !isStreaming) return null;
      return <ThinkingPartCard text={part.text} isStreaming={isStreaming} />;
    case 'dynamic-tool': {
      const dp = part as DynamicToolUIPart;
      const isPending = dp.toolCallId === pendingToolCallId;
      return <ToolPartCard dp={dp} isPending={isPending} isActiveTurn={isActiveTurn} />;
    }
    case 'step-start':
      return null;
    case 'file': {
      const file = part as { mediaType?: string; url?: string };
      const mediaType = String(file.mediaType || '');
      // Only pictures render inline. Anything else the engine seam starts
      // carrying gets its own affordance rather than an image that resolves
      // to a broken icon.
      if (!mediaType.startsWith('image/')) return null;
      const prefix = `data:${mediaType};base64,`;
      const url = String(file.url || '');
      if (!url.startsWith(prefix)) return null;
      return <MessageImage mediaType={mediaType} base64={url.slice(prefix.length)} />;
    }
    default: {
      const anyPart = part as Record<string, unknown>;
      if (typeof anyPart.type === 'string' && anyPart.type === 'data-process-block' && anyPart.data) {
        return (
          <LazyProcessBlock
            details={anyPart.data as ProcessDetails}
            sessionId={sessionId}
            renderDetails={(messages) => messages.map((message) => (
              <AssistantMessageParts
                key={message.id}
                parts={message.parts}
                partOwnerId={message.id}
              />
            ))}
          />
        );
      }
      if (typeof anyPart.type === 'string' && anyPart.type === 'data-result' && anyPart.data) {
        return <ResultPartCard data={anyPart.data as Record<string, unknown>} />;
      }
      if (typeof anyPart.type === 'string' && anyPart.type === 'data-turn-failure' && anyPart.data) {
        const failureData = anyPart.data as { error?: string; failure_phase?: string };
        return <TurnFailureCard error={failureData.error} />;
      }
      if (typeof anyPart.type === 'string' && anyPart.type === 'data-api-retry' && anyPart.data) {
        return <ApiRetryNote payload={anyPart.data as ApiRetryData} />;
      }
      return null;
    }
  }
}

/**
 * An image inside a message, on the elements kit's `Image`.
 *
 * `Image` takes the picture the way the durable block holds it — base64
 * plus a media type — and builds the data URL itself. Its props come
 * from the SDK's generated-file type, so it also names a `uint8Array` it
 * destructures and never reads; the bytes are in `base64`, and decoding them
 * a second time just to satisfy the name would be work nothing consumes.
 */
function MessageImage({ mediaType, base64 }: { mediaType: string; base64: string }) {
  const { t } = useTranslation();
  return (
    <Image
      data-testid="message-image"
      data-media-type={mediaType}
      base64={base64}
      mediaType={mediaType}
      uint8Array={EMPTY_IMAGE_BYTES}
      alt={t('chat:message.pasted_image')}
      className="max-h-80 border border-border object-contain"
    />
  );
}

const EMPTY_IMAGE_BYTES = new Uint8Array();

export function MarkdownContent({ text, testid }: { text: string; testid?: string }) {
  return (
    <div className="markdown" data-testid={testid}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
        {localizeDisplayText(text)}
      </ReactMarkdown>
    </div>
  );
}

/**
 * A thinking block, on the kit's `Reasoning`.
 *
 * The collapsible, the trigger with its shimmer and chevron, and the
 * open-while-streaming behaviour are upstream's. `getThinkingMessage` is the
 * seam it provides for the label, so this wrapper keeps the copy under `t()`
 * (docs/maintainers/upstream-drift-ledger.md — a product decision goes in a
 * wrapper, not into the vendored file).
 *
 * The label deliberately ignores the duration `Reasoning` measures. That
 * duration exists only for a browser that watched the block stream; this
 * transcript is durable, so the same message on reload would fall back to
 * upstream's "a few seconds" and a reader would see the line change under
 * them for a turn that had not.
 *
 * The panel is a plain `CollapsibleContent`, not `ReasoningContent`, which
 * renders Streamdown. One markdown renderer per transcript: everything else
 * here goes through `MarkdownContent` (react-markdown + rehype-highlight),
 * and that is the only path that applies `localizeDisplayText` to vendor text.
 * `CollapsibleContent` is the panel `ReasoningContent` wraps, so the trigger's
 * `aria-controls` still resolves (§10).
 */
export function ThinkingPartCard({ text, isStreaming }: { text: string; isStreaming?: boolean }) {
  const { t } = useTranslation();

  return (
    <Reasoning
      data-testid="reasoning-part"
      data-chars={text.trim().length}
      isStreaming={isStreaming}
      className={cn(
        'mb-0 overflow-hidden rounded-[var(--radius)] border bg-muted/60 transition-colors',
        isStreaming ? 'border-primary/40' : 'border-border',
      )}
    >
      <ReasoningTrigger
        className="ring-inward px-3 py-2 text-13 hover:bg-accent/40"
        getThinkingMessage={(streaming) => (
          <span className="flex flex-1 items-center gap-2 text-left">
            {streaming && <span className="astra-dot astra-dot--live" />}
            <span className="t-eyebrow">
              {streaming ? <Shimmer duration={1}>{t('chat:message.thinking')}</Shimmer> : t('chat:message.thought_process')}
            </span>
          </span>
        )}
      />
      <CollapsibleContent className="border-t border-border">
        <div className="max-h-[400px] overflow-y-auto px-3 py-2.5">
          <MarkdownContent text={text} />
        </div>
      </CollapsibleContent>
    </Reasoning>
  );
}
