import { cn } from '@/lib/utils';
import { PageShell } from '@/components/shell';
import { StatusPill } from './index';
import type { ConsoleStatus } from './editFields';

/**
 * One record, on its own surface.
 *
 * A detail panel beside the list is the right shape when the reader is
 * comparing records; reading or editing one is the other branch of
 * docs/frontend-design.md §3, and it is what these pages do. The panel could
 * not hold the form anyway: a settings field wants its label beside it and its
 * control wide enough to read the value, which is around 750px, and a panel
 * that wide leaves the list it sits next to too narrow to be worth keeping.
 *
 * So the list hands the record over completely. The way back is the shell's
 * breadcrumb — one trail, not a second "back" bolted onto every detail — and
 * the URL is the record: shareable, reloadable, and what the browser's own
 * Back button already understands.
 */
export function ConsoleRecordPage({
  title,
  lede,
  status,
  actions,
  rail,
  children,
}: {
  /**
   * The record's own name — and while no record names itself, whether the fetch
   * is in flight or has failed, the record's type. Never the id: an opaque key
   * in the heading says nothing and repeats the trail's last segment, which
   * falls back to that same URL segment (§1). Never a progress word either —
   * the heading says which page this is, and the body under it says what state
   * that page is in.
   */
  title: React.ReactNode;
  /** What this record is, in a sentence. */
  lede?: React.ReactNode;
  status?: ConsoleStatus;
  /** Operations on the record (delete, jump to its sessions) — not edits. */
  actions?: React.ReactNode;
  /** Observed facts about the record: what the deployment reports, not settings. */
  rail?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <PageShell>
      <div className="mb-6 flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-3">
            <h1 className="t-h1-tight min-w-0 truncate text-2xl">{title}</h1>
            {status && (
              <StatusPill tone={status.tone} className="shrink-0">
                {status.label}
              </StatusPill>
            )}
          </div>
          {lede && <p className="t-copy mt-2 max-w-[66ch] text-muted-foreground">{lede}</p>}
        </div>
        {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
      </div>

      {/* The rail drops under the cards before it would squeeze them: the cards
          hold the form, and a form that has to wrap its own labels has lost
          more than the rail gains by staying beside it. */}
      <div className={cn('flex flex-col gap-6', rail && 'lg:flex-row lg:items-start')}>
        <div className="flex min-w-0 flex-1 flex-col gap-4">{children}</div>
        {rail}
      </div>
    </PageShell>
  );
}
