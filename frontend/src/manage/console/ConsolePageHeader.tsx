import { cn } from '@/lib/utils';

/**
 * Quiet summary line beside a page title — "1 environment · 1 enabled",
 * "N sessions · M running".
 *
 * Not mono: a count is not a machine identity (docs/frontend-design.md §6). It
 * keeps tabular figures because these numbers change under the reader as
 * filters and polls land, and proportional digits make the line twitch.
 */
export function MetaLine({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <span className={cn('t-label-sm text-muted-foreground tabular-nums', className)}>
      {children}
    </span>
  );
}

/**
 * Console page header: the page title, a meta summary line on the same
 * baseline, a short description, and a right-aligned action slot.
 *
 * `.t-h1-tight` owns the title's weight and tracking, so hierarchy stays a
 * matter of size and space rather than of weight (docs/frontend-design.md §0).
 *
 * There is deliberately no eyebrow: it would render an uppercase echo of the
 * heading directly beneath it — a screen states where you are once (§1).
 */
export function ConsolePageHeader({
  title,
  meta,
  description,
  actions,
  className,
}: {
  title: React.ReactNode;
  meta?: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  className?: string;
}) {
  // Two rows below `sm`: side by side, the title block is squeezed to a measure
  // narrower than its own words and the description wraps every three words
  // while the actions hold their width.
  return (
    <div
      className={cn(
        'mb-6 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between sm:gap-4',
        className,
      )}
    >
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-3">
          <h1 className="t-h1-tight text-2xl">{title}</h1>
          {meta && <MetaLine>{meta}</MetaLine>}
        </div>
        {/* Capped at a readable measure rather than the column's full width:
            the description is prose, and prose set across a wide table's track
            wraps at a line length nobody reads to the end of. */}
        {description && (
          <p className="t-copy mt-1.5 max-w-[66ch] text-muted-foreground">{description}</p>
        )}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  );
}
