import { useCallback, useMemo, useState } from 'react';
import { ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { adminGetSessionTrace } from '@/api';
import type { AdminSessionTrace, AdminTracePart } from '@/types';

import { ErrorNote } from '@/components/shell';
import { ConsoleEmptyState } from './console';
import { formatDateTime } from './agentConfig';

/**
 * One response, part by part, with the payload each part actually carried.
 *
 * The digest above answers "what was it doing" at a glance. This answers "what
 * exactly did that response emit", which is a different question and the only
 * one that can be settled by reading — a preview line is a summary of the
 * thing, and the reason an operator opens a part is that the summary was not
 * enough.
 *
 * The parts are fetched per response rather than all at once: the server
 * bounds one response's parts and says when it truncated, and a session's whole
 * output has no bound at all.
 *
 * Rendered as the session's reader stored it. A part's `type` is the AI SDK's
 * word, so it is shown in mono — the reader will match it against a log line —
 * and the preview is marked as quoted for the same reason `last_error` is: it
 * is text this console did not write (docs/frontend-design.md §6, §8). The
 * sequence number is a count and stays sans with `tabular-nums`; mono means
 * "you will paste this", which a row number is not.
 */
function PartRow({ part, index }: { part: AdminTracePart; index: number }) {
  const { t } = useTranslation();
  const preview = String(part.text_preview || part.text || '');
  let raw = '';
  try {
    raw = JSON.stringify(part.raw ?? part, null, 2);
  } catch {
    raw = String(part.raw ?? part);
  }

  return (
    <Collapsible className="console-row">
      {/* Root + Trigger + Content, all three. A trigger without its content
          still renders something that opens, so it looks finished — and every
          trigger advertises an `aria-controls` for a panel that is not on the
          page, which is a lie only a screen reader hears (§10). */}
      <CollapsibleTrigger className="console-row--click ring-inward group flex w-full items-center gap-3 px-3 py-1.5 text-left">
        {/* The open state is the trigger's own `data-panel-open` — Base UI puts
            it on the element that names the panel, and the chevron reads it off
            the group it sits in. */}
        <ChevronRight className="console-row-chevron size-3.5 shrink-0 transition-transform group-data-panel-open:rotate-90" />
        <span className="w-10 shrink-0 tabular-nums text-11 text-muted-foreground">
          {part.seq ?? index}
        </span>
        <span className="t-mono shrink-0 text-11 text-foreground">{part.type || '—'}</span>
        <span
          data-slot="verbatim"
          className="min-w-0 flex-1 truncate text-11 text-muted-foreground"
          title={preview}
        >
          {preview || t('manage:sessions.part_no_preview')}
        </span>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <pre tabIndex={0} className="console-scroll console-val mx-3 mb-2 max-h-72 select-text overflow-auto whitespace-pre-wrap break-words rounded-md border bg-muted/40 px-2.5 py-2 text-11 leading-5 text-foreground">
          {raw}
        </pre>
      </CollapsibleContent>
    </Collapsible>
  );
}

export function ResponseParts({ sessionId, trace }: { sessionId: string; trace: AdminSessionTrace }) {
  const { t } = useTranslation();
  const responses = trace.responses;

  const [responseId, setResponseId] = useState(trace.selected_response_id || '');
  const [parts, setParts] = useState<AdminTracePart[]>(trace.parts);
  const [truncated, setTruncated] = useState(trace.truncated_part_count);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  // The trigger shows what the menu offered, not the id behind it: `items` is
  // where `<SelectValue>` reads the selected response's label from, and
  // without it a selected response collapses to its raw id.
  const responseOptions = useMemo(
    () =>
      responses.map((response) => ({
        value: response.response_id,
        label: t('manage:sessions.response_option', {
          at: formatDateTime(response.started_at ?? undefined),
          outcome: response.outcome || response.status || '—',
        }),
      })),
    [responses, t],
  );

  const selectResponse = useCallback(
    async (next: string) => {
      setResponseId(next);
      setLoading(true);
      try {
        const fetched = await adminGetSessionTrace(sessionId, { responseId: next });
        setParts(fetched.parts);
        setTruncated(fetched.truncated_part_count);
        setError('');
      } catch (e) {
        // The response's parts are what this panel is for, so a failed read
        // says so and keeps the selector usable rather than showing the
        // previous response's parts under the new response's name.
        setParts([]);
        setError((e as Error).message || t('manage:sessions.parts_load_failed'));
      }
      setLoading(false);
    },
    [sessionId, t],
  );

  return (
    <div className="mt-0.5 space-y-2">
      {/* §5: one response is not a choice. The parts below are that
          response's either way, and a select that cannot change them is a
          control that lies. */}
      {responses.length > 1 && (
        <Select
          value={responseId}
          items={responseOptions}
          onValueChange={(next) => {
            // Base UI reports a cleared select as `null`. This one has no null
            // item and no reset, so a null names no response to fetch.
            if (next === null) return;
            void selectResponse(next);
          }}
        >
          <SelectTrigger
            className="w-full max-w-md"
            aria-label={t('manage:sessions.response_placeholder')}
          >
            <SelectValue placeholder={t('manage:sessions.response_placeholder')} />
          </SelectTrigger>
          {/* The menu hangs off the trigger's box; it does not sit on top of
              it. Base UI's default `alignItemWithTrigger` is the macOS
              native-select placement, which puts the SELECTED ITEM's text on
              the trigger's text and leaves the two boxes offset — a menu that
              reads as having missed. `align="start"` keeps the left edges
              together. */}
          <SelectContent align="start" alignItemWithTrigger={false}>
            {responseOptions.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}

      {error ? (
        <ErrorNote>{error}</ErrorNote>
      ) : loading ? (
        <p className="t-copy-sm text-muted-foreground">{t('common:loading')}</p>
      ) : parts.length === 0 ? (
        <ConsoleEmptyState
          title={t('manage:sessions.no_parts')}
          hint={t('manage:sessions.no_parts_hint')}
        />
      ) : (
        <>
          <div className="overflow-hidden rounded-md border">
            {parts.map((part, i) => (
              <PartRow key={`${part.seq ?? 'n'}-${part.type ?? 'p'}-${i}`} part={part} index={i} />
            ))}
          </div>
          {/* Never silently: a bounded window presented as the whole response
              is the one reading of this list that would be wrong. */}
          {truncated > 0 && (
            <p className="t-copy-sm text-citrine-fg">
              {t('manage:sessions.parts_truncated', { count: truncated })}
            </p>
          )}
        </>
      )}
    </div>
  );
}
