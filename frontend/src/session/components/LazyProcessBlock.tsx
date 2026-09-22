import React, { createContext, useContext, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { UIMessage as SDKUIMessage } from 'ai';
import { ChevronDownIcon, ListTreeIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { ErrorNote } from '@/components/shell';
import { Spinner } from '@/components/ui/spinner';
import { useTranscriptAccess } from '../TranscriptAccess';
import type { ProcessDetails, ProcessSummaryState } from '../../types';
import { messageRecordsToSdkMessages } from '../hooks/useInitialMessages';

/**
 * The blocks already fetched for a folded header, shared across the transcript.
 *
 * The list virtualizes, so a header scrolled out of view and back is a fresh
 * component over the same block. Holding the in-flight promise rather than the
 * result means a header reopened while its first read is still running joins
 * that read instead of issuing a second one. The key carries the cursor: the
 * same header read at two checkpoints stands for two different sets of blocks.
 */
export type ProcessDetailsCacheValue = Map<string, Promise<SDKUIMessage[]>>;
export const ProcessDetailsCache = createContext<ProcessDetailsCacheValue | null>(null);

/**
 * The label each settled response has been given, keyed by its message id.
 *
 * Held beside the details for the same reason: a folded header is asked for
 * its label when it mounts, and the list mounts it again every time it scrolls
 * back into view. A label already answered is reused rather than asked for.
 */
export type ProcessSummaryCacheValue = Map<string, ProcessSummaryState>;
export const ProcessSummaryCache = createContext<ProcessSummaryCacheValue | null>(null);

const PROCESS_SUMMARY_POLL_MS = 1000;

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason ?? '');
}

/**
 * One folded process, opened on demand.
 *
 * The page it arrived on carried the header only; the blocks it stands for are
 * a second read, made the first time a reader opens it and pinned to the cursor
 * the header was issued with, so what opens is what that page folded rather
 * than whatever the record holds after later turns.
 */
export function LazyProcessBlock({
  details,
  sessionId,
  renderDetails,
}: {
  details: ProcessDetails;
  sessionId?: string;
  renderDetails: (messages: SDKUIMessage[]) => React.ReactNode;
}) {
  const { t } = useTranslation();
  const { readDetails, generateSummary } = useTranscriptAccess();
  const ownCache = useRef<ProcessDetailsCacheValue>(new Map());
  const cache = useContext(ProcessDetailsCache) ?? ownCache.current;
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<SDKUIMessage[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [summary, setSummary] = useState<ProcessSummaryState | null>(details.summary ?? null);
  const key = `${details.session_id}:${details.block_id}:${details.cursor}`;

  useEffect(() => {
    setMessages(null);
    setError(null);
  }, [key]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    let pending = cache.get(key);
    if (!pending) {
      pending = readDetails(details.session_id, details.block_id, details.cursor)
        .then((page) => messageRecordsToSdkMessages(page.messages, null));
      cache.set(key, pending);
      // A failed read is not an answer to cache: the retry below has to reach
      // the server rather than be handed the same rejection again.
      pending.catch(() => { cache.delete(key); });
    }
    setError(null);
    void pending.then(
      (value) => { if (!cancelled) setMessages(value); },
      (reason) => { if (!cancelled) setError(errorMessage(reason)); },
    );
    return () => { cancelled = true; };
  }, [cache, key, open, attempt, details.session_id, details.block_id, details.cursor, readDetails]);

  useEffect(() => {
    setSummary(details.summary ?? null);
    if (!sessionId || !details.summarize || !generateSummary) return;
    if (details.summary && details.summary.status !== 'generating') return;
    let cancelled = false;
    let timer: number | undefined;
    const ask = async () => {
      try {
        const state = await generateSummary(details.session_id, details.message_id);
        if (cancelled) return;
        setSummary(state);
        if (state.status === 'generating') {
          timer = window.setTimeout(() => { void ask(); }, PROCESS_SUMMARY_POLL_MS);
        }
      } catch {
        // The work behind the header is intact whether or not it has a label,
        // so a header that could not be named keeps its generic title instead
        // of putting a failure into the transcript.
        if (!cancelled) setSummary({ status: 'failed' });
      }
    };
    setSummary({ status: 'generating' });
    void ask();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [sessionId, details.session_id, details.message_id, details.summarize, details.summary, generateSummary]);

  const summaryText = String(summary?.summary ?? '').trim();
  const title = summaryText
    || (summary?.status === 'generating' ? t('chat:process.summary_generating') : t('chat:process.title'));

  return (
    <Collapsible
      className="group w-full min-w-0 overflow-hidden rounded-[var(--radius)] border border-border bg-muted/30"
      data-testid="assistant-turn-process"
      data-process-block-id={details.block_id}
      open={open}
      onOpenChange={setOpen}
    >
      <CollapsibleTrigger
        data-testid="assistant-turn-process-trigger"
        className="ring-inward flex w-full items-center gap-2.5 p-3 text-left text-sm hover:bg-accent/40"
      >
        <span className="flex size-7 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground">
          <ListTreeIcon aria-hidden="true" className="size-4" />
        </span>
        <span className="min-w-0 flex-1 truncate font-medium text-muted-foreground">{title}</span>
        {!details.summarize && (
          <span className="shrink-0 text-xs text-muted-foreground">
            {details.tool_count
              ? t('chat:process.tool_calls', { count: details.tool_count })
              : t('chat:process.reasoning_only')}
          </span>
        )}
        <ChevronDownIcon
          aria-hidden="true"
          className="size-4 shrink-0 text-muted-foreground transition-transform group-data-open:rotate-180"
        />
      </CollapsibleTrigger>
      <CollapsibleContent
        data-testid="process-block-details"
        className="flex min-w-0 flex-col gap-2.5 border-t border-border px-3 py-3"
      >
        {error !== null ? (
          <ErrorNote className="flex items-center justify-between gap-2">
            <span>{t('chat:process.details_failed', { error })}</span>
            <Button size="sm" variant="secondary" onClick={() => setAttempt((value) => value + 1)}>
              {t('common:retry')}
            </Button>
          </ErrorNote>
        ) : messages ? renderDetails(messages) : (
          <div role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
            <Spinner />
            {t('chat:process.details_loading')}
          </div>
        )}
      </CollapsibleContent>
    </Collapsible>
  );
}
