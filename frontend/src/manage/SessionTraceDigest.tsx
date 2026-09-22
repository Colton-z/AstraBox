import { ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import type { AdminSessionTrace, AdminTraceMessage } from '@/types';

import { formatDateTime } from './agentConfig';

/** The persisted message document, or `''` when the server sent no raw. */
function rawOf(message: AdminTraceMessage): string {
  if (message.raw == null) return '';
  try {
    return JSON.stringify(message.raw, null, 2);
  } catch {
    return String(message.raw);
  }
}

/**
 * The tail of a session's conversation — the last few messages, each a role, a
 * time and one line of what was said.
 *
 * Not the transcript: it answers "what was it doing when it stopped" at a
 * glance, and reading the run properly is a deeper drill the operator opens
 * deliberately. It lives here rather than inside a page because the list and
 * the record both show it, and a copy in each is a copy that drifts.
 *
 * A preview line is a summary, and the reason anyone opens a message is that
 * the summary was not enough — so a message the server sent `raw` for opens to
 * the document it was summarised from. A message with no raw stays a plain row
 * rather than growing a control that reveals nothing (§5).
 */
export function TraceDigest({ trace }: { trace: AdminSessionTrace }) {
  const { t } = useTranslation();
  const recent = (trace.messages ?? []).slice(-6);
  if (recent.length === 0) {
    return <span className="text-muted-foreground">{t('manage:sessions.trace_no_messages')}</span>;
  }
  return (
    <div className="mt-0.5 space-y-1.5 rounded-md border bg-muted/40 px-2.5 py-2">
      {recent.map((m, i) => {
        const raw = rawOf(m);
        const head = (
          <>
            <div className="text-11 tabular-nums text-muted-foreground">
              {(m.role || '—')} · {formatDateTime(m.created_at)}
            </div>
            {m.content_preview && (
              <div data-slot="verbatim" className="truncate text-xs leading-snug text-foreground">
                {m.content_preview}
              </div>
            )}
          </>
        );
        const key = `${m.turn_id || 'no-turn'}-${i}`;
        if (!raw) {
          return (
            <div key={key} className="min-w-0">
              {head}
            </div>
          );
        }
        return (
          <Collapsible key={key} className="min-w-0">
            <CollapsibleTrigger
              aria-label={
                m.content_preview
                || t('manage:sessions.open_raw_message', { role: m.role || '—' })
              }
              className="group flex w-full items-start gap-1.5 text-left"
            >
              {/* The group is the Base UI collapsible trigger, which marks
                  itself `data-panel-open`. Radix's `data-state=open` matches
                  nothing on it, so a selector written that way leaves this
                  arrow unturned. */}
              <ChevronRight className="console-row-chevron mt-0.5 size-3.5 shrink-0 transition-transform group-data-panel-open:rotate-90" />
              <span className="min-w-0 flex-1">{head}</span>
            </CollapsibleTrigger>
            <CollapsibleContent>
              <pre tabIndex={0} className="console-scroll console-val mt-1 max-h-72 select-text overflow-auto whitespace-pre-wrap break-words rounded-md border bg-background px-2.5 py-2 text-11 leading-5 text-foreground">
                {raw}
              </pre>
            </CollapsibleContent>
          </Collapsible>
        );
      })}
    </div>
  );
}
