import { cn } from '@/lib/utils';

/**
 * A single row that declares which part gives way when it runs out of width.
 *
 * A row of fixed and flexible parts does not shrink correctly by default: a
 * flex or grid child's `min-width` is `auto`, so it refuses to go below its own
 * content and pushes the excess onto an ancestor, where it is cut with no
 * ellipsis — the ellipsis is drawn by whoever does the clipping, and that is
 * an ancestor, not this row.
 *
 * A long status badge can otherwise push the title's ellipsis outside the
 * visible row while a short badge appears correct.
 *
 * The row is a grid rather than flex because the tracks say the intent
 * outright: `auto` for the parts that keep their size, `minmax(0,1fr)` for the
 * one that yields. Whatever goes in the yielding slot still has to be able to
 * shrink — pair it with `Ellipsis`.
 *
 * The yielding cell is itself a grid, and that is load-bearing rather than
 * decorative. A block cell hands its child the cell's size to lay out against,
 * and an inline-level child then sizes itself fit-content — whose floor is its
 * own min-content, which `white-space: nowrap` makes equal to its max-content.
 * Such a child cannot be squeezed at all: it draws past the cell and over
 * whatever is in `trail`. A single `minmax(0,max-content)` track makes the
 * child a grid item stretched to a track that can never exceed the cell, so the
 * width is decided here and the child has only to clip.
 *
 * The case this covers is a status pill: `inline-flex`, `nowrap`, and
 * `truncate` on the label. Nothing on the pill itself fixes it — `max-width:
 * 100%`, `width: fit-content` and `min-width: 0` cannot lower a fit-content
 * floor.
 */
export function TruncatingRow({
  children,
  lead,
  trail,
  className,
}: {
  /** The part that gives way. */
  children: React.ReactNode;
  /** Keeps its width, on the left — an icon or avatar. */
  lead?: React.ReactNode;
  /** Keeps its width, on the right — a badge, a timestamp, an action. */
  trail?: React.ReactNode;
  className?: string;
}) {
  const template = [lead ? 'auto' : null, 'minmax(0,1fr)', trail ? 'auto' : null]
    .filter(Boolean)
    .join(' ');

  return (
    <div
      data-slot="truncating-row"
      className={cn('grid min-w-0 items-center gap-2', className)}
      style={{ gridTemplateColumns: template }}
    >
      {lead}
      <div
        data-slot="row-yield"
        className="grid min-w-0 grid-cols-[minmax(0,max-content)]"
      >
        {children}
      </div>
      {trail}
    </div>
  );
}
