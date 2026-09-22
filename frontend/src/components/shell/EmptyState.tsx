import { cn } from '@/lib/utils';
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from '@/components/ui/empty';

/**
 * Nothing here yet, said the same way everywhere.
 *
 * A shape, not a look: an icon in a hairline tile, what will appear here, and
 * what produces it. Every panel that can be empty renders this one component,
 * so panels a reader switches between with a tab do not each describe emptiness
 * with a different icon size and frame.
 *
 * Both surfaces stand on the kit's `Empty` — this one and the console's
 * `ConsoleEmptyState`, which adds the mark and the star-field over the same
 * parts. Padding, measure, type and the gaps between the parts therefore belong
 * to the kit rather than to either caller, which is what stops the two drifting
 * apart in any of them. What is left here is this surface's own skin: the
 * hairline tile, and a slot for a backdrop behind the content.
 */
export function EmptyState({
  icon,
  title,
  hint,
  action,
  className,
  children,
}: {
  /** What this is about — a folder for files, a bot for agents. */
  icon?: React.ReactNode;
  title: React.ReactNode;
  /** What will appear here and what produces it (docs/frontend-design.md §5). */
  hint?: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
  /** A backdrop, for a skin that wants one behind the content. */
  children?: React.ReactNode;
}) {
  return (
    // A handle that is not copy. "The empty state is gone" is an assertion of
    // absence, and one keyed on wording is satisfied by the wording changing —
    // it stops meaning anything without ever failing. Scope it to the panel
    // being asked about; the name is shared by every empty state on purpose.
    // `flex-initial` puts back the kit's default flex behaviour: `Empty` asks
    // to grow, and every caller here hands it to a parent that already owns its
    // own height and centres what it holds, so a box that ate the free space
    // would move the copy in the parents that have any to give.
    <Empty
      data-testid="empty-state"
      className={cn('relative flex-initial overflow-hidden', className)}
    >
      {children}
      {/* `relative` on the parts, not only on the frame: a backdrop arrives as
          children and positions itself, and a positioned element paints over
          its static siblings whatever the source order — without this the
          backdrop covers the words. */}
      <EmptyHeader className="relative">
        {/* The default media variant, not `icon`: that variant forces any svg
            without a `size-` class to 16px, while callers size their own glyph.
            The shared hairline frame gives all empty states the same visual
            treatment. */}
        {icon && (
          <EmptyMedia className="size-11 rounded-lg border bg-card text-muted-foreground">
            {icon}
          </EmptyMedia>
        )}
        {/* Stated rather than inherited: these panels sit on grounds that set
            their own muted body colour, and a title that quietly took it would
            stop being the thing read first. */}
        <EmptyTitle className="text-foreground">{title}</EmptyTitle>
        {hint && <EmptyDescription>{hint}</EmptyDescription>}
      </EmptyHeader>
      {action && <EmptyContent className="relative">{action}</EmptyContent>}
    </Empty>
  );
}
