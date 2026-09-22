import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Message, MessageContent } from '@/components/ai-elements/message';
import type { ContentBlock } from '@/types';
import { blocksToSDKParts } from '../hooks/useInitialMessages';
import { PartRenderer } from './MessageParts';
import { cn } from '@/lib/utils';
import { BotIcon, XIcon } from 'lucide-react';
import { ErrorNote } from '@/components/shell';
import {
  getSessionChildRunMessages,
  type ChildRunTranscriptMessage,
} from '@/api';
import {
  subagentDisplayStatus,
  type SubagentEntry,
} from '../hooks/useSubagentRegistry';
import { shortSubagentId } from './AgentsListPanel';
import { keepsLastRead } from '@/hooks/useKeepCurrent';

interface SubagentTranscriptDrawerProps {
  sessionId: string;
  refreshRevision: number;
  backgroundTasksPending: boolean;
  childRunId: string | null;
  entry: SubagentEntry | undefined;
  onClose: () => void;
  onStopChildRun?: (childRunId: string) => Promise<void>;
}

export function SubagentTranscriptDrawer({
  sessionId,
  refreshRevision,
  backgroundTasksPending,
  childRunId,
  entry,
  onClose,
  onStopChildRun,
}: SubagentTranscriptDrawerProps) {
  const { t } = useTranslation();
  const open = Boolean(childRunId);
  const [transcript, setTranscript] = useState<ChildRunTranscriptMessage[]>([]);
  const [transcriptError, setTranscriptError] = useState<string | null>(null);
  const [stopPending, setStopPending] = useState(false);
  const [stopError, setStopError] = useState<string | null>(null);
  const requestGeneration = useRef(0);
  const loadedTranscript = useRef(false);
  const refreshTranscript = useCallback(async (manual = false) => {
    const selectedChildRunId = String(childRunId ?? '').trim();
    if (!selectedChildRunId) return;
    const generation = ++requestGeneration.current;
    try {
      const page = await getSessionChildRunMessages(sessionId, selectedChildRunId);
      if (generation !== requestGeneration.current) return;
      setTranscript(page.messages);
      loadedTranscript.current = true;
      setTranscriptError(null);
    } catch (error) {
      if (generation !== requestGeneration.current) return;
      if (!keepsLastRead(error, { background: !manual }, loadedTranscript.current)) {
        setTranscriptError(error instanceof Error ? error.message : String(error));
      }
    }
  }, [childRunId, sessionId]);

  useEffect(() => {
    requestGeneration.current += 1;
    loadedTranscript.current = false;
    setTranscript([]);
    setTranscriptError(null);
    setStopPending(false);
    setStopError(null);
  }, [childRunId, sessionId]);

  useEffect(() => {
    if (!open) return;
    void refreshTranscript();
  }, [backgroundTasksPending, entry?.active, open, refreshRevision, refreshTranscript]);

  useEffect(() => {
    if (!open) return;
    const reconnect = () => { void refreshTranscript(); };
    window.addEventListener('online', reconnect);
    return () => window.removeEventListener('online', reconnect);
  }, [open, refreshTranscript]);

  // A continuable child can be idle; poll for activity, not resumability.
  useEffect(() => {
    if (!open || (!entry?.active && !backgroundTasksPending)) return;
    const timer = window.setInterval(() => { void refreshTranscript(); }, 2500);
    return () => window.clearInterval(timer);
  }, [backgroundTasksPending, entry?.active, open, refreshTranscript]);

  const messages = useMemo(() => {
    // Results may arrive in a later message. The shared converter pairs them
    // only against this child's records, never the root or a sibling's tools.
    const typedTranscript = transcript.map((message) => ({
      ...message, content: message.content as unknown as ContentBlock[],
    }));
    const blocks = typedTranscript.flatMap((message) => message.content);
    const converted = typedTranscript.map((message, index) => ({
      id: message.message_id ?? String(index),
      role: message.role,
      parts: blocksToSDKParts(message.content, null, blocks)
        .filter((part) => part.type !== 'text' || part.text.trim().length > 0),
    })).filter((message) => message.parts.length > 0);
    if (converted.length > 0) return converted;
    const summary = entry?.summary?.trim();
    if (!summary) return converted;
    return [{
      id: 'lifecycle-summary',
      role: 'assistant' as const,
      parts: blocksToSDKParts([{ type: 'text', text: summary }]),
    }];
  }, [entry?.summary, transcript]);

  const handleStop = useCallback(async () => {
    const selectedChildRunId = String(childRunId ?? '').trim();
    if (!selectedChildRunId || !onStopChildRun || stopPending) return;
    setStopPending(true);
    setStopError(null);
    try {
      await onStopChildRun(selectedChildRunId);
    } catch (error) {
      setStopError(error instanceof Error ? error.message : String(error));
    } finally {
      setStopPending(false);
    }
  }, [childRunId, onStopChildRun, stopPending]);

  if (!open) return null;

  const title = entry?.description?.trim() || `Subagent ${shortSubagentId(childRunId ?? '')}`;
  const status = entry ? subagentDisplayStatus(entry) : '';

  /*
    Overlay the transcript so inspecting a child does not narrow the main
    reading column. Position against the transcript rather than the viewport
    to keep the composer usable. A portalled SheetOverlay would intercept
    clicks on the composer even when its controls remained visible.
  */
  return (
    <div
      data-testid="subagent-transcript-drawer"
      className={cn(
        'absolute inset-y-0 right-0 z-30 flex w-1/2 min-w-[20rem] flex-col border-l border-border bg-background shadow-xl',
        'animate-in slide-in-from-right',
      )}
      role="dialog"
      aria-label={t('chat:subagent.transcript_aria')}
    >
      <header className="flex shrink-0 items-start justify-between gap-2 border-b border-border p-3">
        <div className="flex min-w-0 flex-1 items-start gap-2">
          <BotIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5">
              <span className="truncate text-sm font-semibold" title={title}>
                {title}
              </span>
              <span className="shrink-0 rounded bg-muted px-1 font-mono text-10 text-muted-foreground">
                {shortSubagentId(childRunId ?? '')}
              </span>
              {status && (
                <Badge variant={entry?.active ? 'default' : 'outline'} className="h-5 text-10">
                  {status}
                </Badge>
              )}
            </div>
            {entry?.summary && (
              <p className="mt-0.5 truncate text-xs text-muted-foreground" title={entry.summary}>
                {entry.summary}
              </p>
            )}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {entry?.operations.includes('stop') && childRunId && onStopChildRun && (
            <Button
              variant="outline"
              size="sm"
              className="h-7 text-xs"
              disabled={stopPending}
              onClick={() => { void handleStop(); }}
              aria-label={t('chat:subagent.stop_aria')}
              data-testid="subagent-stop-button"
            >
              {stopPending ? t('chat:subagent.stopping') : t('chat:subagent.stop')}
            </Button>
          )}
          {/* Both handles, for two different readers: `aria-label` names the
              control for anyone using the console, and the testid names it for
              a test. Locating this button by its label made three specs fail
              together the day the label's wording changed. */}
          <Button
            variant="ghost"
            size="icon"
            onClick={onClose}
            aria-label={t('chat:subagent.close_aria')}
            data-testid="subagent-close-button"
          >
            <XIcon className="size-4" />
          </Button>
        </div>
      </header>

      {/* Use a plain overflow-y-auto wrapper instead of Radix ScrollArea —
          ScrollArea's viewport allows horizontal overflow, which lets long
          single-line JSON in ToolInput push the inner CodeBlock past the
          drawer's right edge. A `min-w-0 overflow-x-hidden` container
          properly constrains width down the flex chain. */}
      <div className="min-w-0 flex-1 overflow-y-auto overflow-x-hidden">
        {/* A gap column, not `space-y-3`: Tailwind v4 emits `space-y` under
            `:where()`, so any child with its own margin utility outranks it —
            and `ThinkingPartCard` carries `mb-0` to cancel the kit's `mb-4`.
            The main pane's `MessageContent` spaces its parts with `gap` for
            the same reason (docs/frontend-design.md §0). */}
        <div
          data-testid="subagent-transcript-column"
          className="flex min-w-0 max-w-full flex-col gap-3 overflow-x-hidden p-3"
        >
          {stopError && (
            <ErrorNote>{t('chat:subagent.stop_failed')}: {stopError}</ErrorNote>
          )}
          {transcriptError ? (
            <ErrorNote>
              <div>{t('chat:subagent.transcript_failed')}: {transcriptError}</div>
              <Button
                variant="outline"
                size="sm"
                className="mt-2"
                onClick={() => { void refreshTranscript(true); }}
              >
                {t('chat:subagent.retry')}
              </Button>
            </ErrorNote>
          ) : messages.length === 0 ? (
            <p className="text-center text-xs text-muted-foreground">
              {entry?.active
                ? t('chat:subagent.waiting_output')
                : t('chat:subagent.no_transcript')}
            </p>
          ) : (
            messages.map((message) => (
              <Message key={`${childRunId}:${message.id}`} from={message.role}
                data-role={message.role} className="min-w-0 max-w-full">
                <MessageContent className="w-full min-w-0 max-w-full gap-3 overflow-hidden">
                  {message.parts.map((part, index) => (
                    <div key={part.type === 'dynamic-tool' ? part.toolCallId : `${part.type}:${index}`}
                      className="min-w-0 max-w-full overflow-x-auto break-words">
                      <PartRenderer part={part} isActiveTurn={entry?.active === true}
                        isUser={message.role === 'user'} />
                    </div>
                  ))}
                </MessageContent>
              </Message>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
