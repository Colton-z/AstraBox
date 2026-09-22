import { useCallback, useMemo, useState } from 'react';
import { ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { adminGetSessionTrace } from '@/api';
import type { AdminSessionTrace, AdminTraceFrame } from '@/types';

import { ErrorNote } from '@/components/shell';
import { ConsoleEmptyState } from './console';
import { formatDateTime } from './agentConfig';

/**
 * One turn, frame by frame, with the payload each frame actually carried.
 *
 * The digest above answers "what was it doing" at a glance. This answers "what
 * exactly did that turn emit", which is a different question and the only one
 * that can be settled by reading — a preview line is a summary of the thing,
 * and the reason an operator opens a frame is that the summary was not enough.
 *
 * The frames are fetched per turn rather than all at once: the server bounds
 * one turn's frames and says when it truncated, and a session's whole frame
 * history has no bound at all.
 *
 * Rendered as the engine produced it. A frame's `type` is the SDK's word, so it
 * is shown in mono — the reader will match it against a log line — and the
 * preview is marked as quoted for the same reason `last_error` is: it is text
 * this console did not write (docs/frontend-design.md §6, §8). The sequence
 * number is a count and stays sans with `tabular-nums`; mono means "you will
 * paste this", which a row number is not.
 */
function FrameRow({ frame, index }: { frame: AdminTraceFrame; index: number }) {
  const { t } = useTranslation();
  const preview = String(frame.text_preview || frame.text || '');
  let raw = '';
  try {
    raw = JSON.stringify(frame, null, 2);
  } catch {
    raw = String(frame);
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
          {frame.seq ?? index}
        </span>
        <span className="t-mono shrink-0 text-11 text-foreground">{frame.type || '—'}</span>
        <span
          data-slot="verbatim"
          className="min-w-0 flex-1 truncate text-11 text-muted-foreground"
          title={preview}
        >
          {preview || t('manage:sessions.frame_no_preview')}
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

export function TurnFrames({ sessionId, trace }: { sessionId: string; trace: AdminSessionTrace }) {
  const { t } = useTranslation();
  const turns = trace.turns ?? [];

  const [turnId, setTurnId] = useState(trace.selected_turn_id || trace.current_turn_id || '');
  const [frames, setFrames] = useState<AdminTraceFrame[]>(trace.frames ?? []);
  const [truncated, setTruncated] = useState(trace.truncated_frame_count ?? 0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  // The trigger shows what the menu offered, not the id behind it: `items` is
  // where `<SelectValue>` reads the selected turn's label from, and without it
  // a selected turn collapses to its raw `turn_id`.
  const turnOptions = useMemo(
    () =>
      turns.map((turn) => ({
        value: turn.turn_id,
        label: t('manage:sessions.turn_option', {
          at: formatDateTime(turn.latest_created_at),
          count: turn.message_count ?? 0,
        }),
      })),
    [turns, t],
  );

  const selectTurn = useCallback(
    async (next: string) => {
      setTurnId(next);
      setLoading(true);
      try {
        const fetched = await adminGetSessionTrace(sessionId, { turnId: next });
        setFrames(fetched.frames ?? []);
        setTruncated(fetched.truncated_frame_count ?? 0);
        setError('');
      } catch (e) {
        // The turn's frames are what this panel is for, so a failed read says
        // so and keeps the selector usable rather than showing the previous
        // turn's frames under the new turn's name.
        setFrames([]);
        setError((e as Error).message || t('manage:sessions.frames_load_failed'));
      }
      setLoading(false);
    },
    [sessionId, t],
  );

  return (
    <div className="mt-0.5 space-y-2">
      {/* §5: one turn is not a choice. The frames below are that turn's either
          way, and a select that cannot change them is a control that lies. */}
      {turns.length > 1 && (
        <Select
          value={turnId}
          items={turnOptions}
          onValueChange={(next) => {
            // Base UI reports a cleared select as `null`. This one has no null
            // item and no reset, so a null names no turn to fetch.
            if (next === null) return;
            void selectTurn(next);
          }}
        >
          <SelectTrigger
            className="w-full max-w-md"
            aria-label={t('manage:sessions.turn_placeholder')}
          >
            <SelectValue placeholder={t('manage:sessions.turn_placeholder')} />
          </SelectTrigger>
          {/* The menu hangs off the trigger's box; it does not sit on top of
              it. Base UI's default `alignItemWithTrigger` is the macOS
              native-select placement, which puts the SELECTED ITEM's text on
              the trigger's text and leaves the two boxes offset — a menu that
              reads as having missed. `align="start"` keeps the left edges
              together. */}
          <SelectContent align="start" alignItemWithTrigger={false}>
            {turnOptions.map((option) => (
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
      ) : frames.length === 0 ? (
        <ConsoleEmptyState
          title={t('manage:sessions.no_frames')}
          hint={t('manage:sessions.no_frames_hint')}
        />
      ) : (
        <>
          <div className="overflow-hidden rounded-md border">
            {frames.map((frame, i) => (
              <FrameRow key={`${frame.seq ?? 'n'}-${frame.type ?? 'f'}-${i}`} frame={frame} index={i} />
            ))}
          </div>
          {/* Never silently: a bounded window presented as the whole turn is
              the one reading of this list that would be wrong. */}
          {truncated > 0 && (
            <p className="t-copy-sm text-citrine-fg">
              {t('manage:sessions.frames_truncated', { count: truncated })}
            </p>
          )}
        </>
      )}
    </div>
  );
}
